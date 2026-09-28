import sys

import pytest

from conftest import mini_pair, real_pair
from nhl_dfs.build.candidates import BASE_FAMILY, SolverUnavailable, generate
from nhl_dfs.build.milp import GroupConstraint, overlap_units
from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models.priors import prior_objective, prior_table
from pool_builder import classic_pool, make_pool, sd_person

pytestmark = pytest.mark.c2a


def _load(pair):
    pool = read_salary(pair[0])
    return pool, prior_objective(pool, prior_table(pool))


def _min_diff(pool, cands):
    units = [overlap_units([pool.by_role_id[r] for r in c.role_ids], pool.mode) for c in cands]
    size = len(cands[0].role_ids)
    return min(size - len(a & b) for i, a in enumerate(units) for b in units[i + 1:])


def _all_legal(pool, cands):
    return all(check_lineup([pool.by_role_id[r] for r in c.role_ids], pool.mode).ok for c in cands)


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_distinct_candidates_are_legal_distinct_and_pairwise_different(mode):
    pool, obj = _load(mini_pair(mode))
    cands = generate(pool, pool.mode, obj, 40, seed=7, perturb_sd=2.0)
    assert len(cands) == 40
    assert len({c.key for c in cands}) == 40
    assert _min_diff(pool, cands) >= 2
    assert _all_legal(pool, cands)
    assert {c.family for c in cands} == {BASE_FAMILY}


def test_objective_value_is_the_unperturbed_objective():
    pool, obj = _load(mini_pair("classic"))
    for c in generate(pool, pool.mode, obj, 10, seed=1, perturb_sd=5.0):
        assert c.objective_value == pytest.approx(sum(obj[r] for r in c.role_ids))


def test_sharp_objective_still_yields_distinct_lineups():
    # With no noise every draw has the same optimum; only the lazily added rows separate them.
    pool, obj = _load(mini_pair("classic"))
    cands = generate(pool, pool.mode, obj, 15, seed=1, perturb_sd=0.0, min_pairwise_diff=3)
    assert len({c.key for c in cands}) == 15
    assert _min_diff(pool, cands) >= 3
    values = [c.objective_value for c in cands]
    # noise-free: each is the next best under the rows (within HiGHS's default 1e-4 gap)
    assert all(b <= a * (1 + 1e-3) for a, b in zip(values, values[1:]))


def test_non_distinct_draws_return_repeats_when_the_objective_is_sharp():
    pool, obj = _load(mini_pair("showdown"))
    cands = generate(pool, pool.mode, obj, 20, seed=3, perturb_sd=0.01, distinct=False)
    assert len(cands) == 20
    assert len({c.key for c in cands}) < 20
    assert _all_legal(pool, cands)


def test_non_distinct_draws_vary_with_real_noise():
    pool, obj = _load(mini_pair("showdown"))
    cands = generate(pool, pool.mode, obj, 20, seed=3, perturb_sd=6.0, distinct=False)
    assert len({c.key for c in cands}) > 1


def test_seed_reproduces_and_changes_the_draws():
    pool, obj = _load(mini_pair("classic"))
    a = generate(pool, pool.mode, obj, 12, seed=11, perturb_sd=3.0)
    b = generate(pool, pool.mode, obj, 12, seed=11, perturb_sd=3.0)
    c = generate(pool, pool.mode, obj, 12, seed=12, perturb_sd=3.0)
    assert [x.key for x in a] == [x.key for x in b]
    assert [x.key for x in a] != [x.key for x in c]


def test_exhaustion_returns_every_lineup_and_stops():
    # 4 AAA + 3 BBB people, all cheap: 7 captains x C(6, 5) FLEX sets = 42 lineups, all legal.
    rows = []
    for i, team in enumerate(["AAA"] * 4 + ["BBB"] * 3):
        rows.extend(sd_person(i, team, "C", 3000))
    pool = make_pool(Mode.SHOWDOWN, rows)
    cands = generate(pool, pool.mode, {}, 500, seed=0, perturb_sd=1.0, min_pairwise_diff=1)
    assert len(cands) == 42
    assert len({c.key for c in cands}) == 42


def test_groups_menu_cycles_families_and_honors_them():
    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    obj = {r.role_id: 1.0 for r in pool.rows}
    aaa = frozenset(r.role_id for r in pool.rows if r.team == "AAA" and not r.is_goalie)
    bbb = frozenset(r.role_id for r in pool.rows if r.team == "BBB" and not r.is_goalie)
    menu = [("aaa4", [GroupConstraint(aaa, min_count=4)]), ("bbb3", [GroupConstraint(bbb, min_count=3)])]
    cands = generate(pool, pool.mode, obj, 10, seed=5, perturb_sd=1.0, groups_menu=menu)
    assert [c.family for c in cands] == ["aaa4", "bbb3"] * 5
    for c in cands:
        need, ids = (4, aaa) if c.family == "aaa4" else (3, bbb)
        assert len(set(c.role_ids) & ids) >= need
    assert _min_diff(pool, cands) >= 2


def test_an_impossible_family_is_skipped():
    pool = classic_pool()
    aaa = frozenset(r.role_id for r in pool.rows if r.team == "AAA")
    menu = [("never", [GroupConstraint(aaa, min_count=20)]), ("any", [])]
    cands = generate(pool, pool.mode, {}, 5, seed=0, perturb_sd=1.0, groups_menu=menu)
    assert len(cands) == 5 and {c.family for c in cands} == {"any"}


def test_spent_budget_returns_nothing_rather_than_anything_unchecked():
    pool, obj = _load(mini_pair("classic"))
    assert generate(pool, pool.mode, obj, 10, seed=0, perturb_sd=1.0, time_limit_total_s=0.0) == []


def test_bad_arguments_raise():
    pool, obj = _load(mini_pair("classic"))
    with pytest.raises(ValueError):
        generate(pool, pool.mode, obj, 5, seed=0, perturb_sd=-1.0)
    with pytest.raises(ValueError):
        generate(pool, pool.mode, obj, 5, seed=0, perturb_sd=1.0, min_pairwise_diff=10)


def test_solver_unavailable_raises_for_the_caller_to_route(monkeypatch):
    pool, obj = _load(mini_pair("classic"))
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    with pytest.raises(SolverUnavailable):
        generate(pool, pool.mode, obj, 5, seed=0, perturb_sd=1.0)


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_pool_gives_150_distinct_legal_candidates(mode):
    # Correctness only. Wall clock is measured by tools/bench_candidates.py and recorded in the
    # tracker; it is kept out of this suite so a slow machine cannot block later chunks.
    pool, obj = _load(real_pair(mode))
    cands = generate(pool, pool.mode, obj, 150, seed=20260929, perturb_sd=2.0, time_limit_total_s=60.0)
    assert len(cands) == 150 and len({c.key for c in cands}) == 150
    assert _min_diff(pool, cands) >= 2
    assert _all_legal(pool, cands)
