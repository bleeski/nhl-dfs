import random
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, mini_pair, real_pair
from nhl_dfs.build.feasible import find_one
from nhl_dfs.build.milp import solve_lineup
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, check_lineup, slot_accepts
from nhl_dfs.contracts.statuses import FeasibleStatus, SearchStatus
from nhl_dfs.intake.salary import read_salary
from pool_builder import classic_pool, make_pool, random_pool, row, showdown_pool

pytestmark = pytest.mark.c2a


def _legal(pool, lineup):
    return check_lineup([pool.by_role_id[x] for x in lineup], pool.mode).ok


def _trap_pool(n_cheap: int):
    """Every cheap skater is on AAA; the only other skaters are two 17000 D-men on BBB and CCC.

    Any 3-team lineup costs 2500 * 7 + 17000 * 2 = 51500 > 50000, but the static bounds
    (cheapest fill 22500, three teams reachable) cannot see it: only exhaustion proves it.
    """
    rows = [row("g", "AAA", "G", 2500)]
    for pos in ("C", "LW", "D"):
        rows += [row(f"{pos}{i}", "AAA", pos, 2500) for i in range(n_cheap)]
    rows += [row("bD", "BBB", "D", 17000), row("cD", "CCC", "D", 17000)]
    return make_pool(Mode.CLASSIC, rows)


@pytest.mark.parametrize("pair", [mini_pair, real_pair], ids=["mini", "real"])
@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_fixture_pools_are_found_and_legal(pair, mode):
    pool = read_salary(pair(mode)[0])
    res = find_one(pool, pool.mode)
    assert res.status is FeasibleStatus.FOUND
    assert _legal(pool, res.lineup)


def test_tiny_trap_pool_is_proven_infeasible_by_exhaustion():
    pool = _trap_pool(3)
    res = find_one(pool, pool.mode, budget_s=5.0)
    assert res.status is FeasibleStatus.INFEASIBLE_PROVEN
    assert res.nodes > 0 and res.detail == "search space exhausted"
    assert solve_lineup(pool, pool.mode, {}).status is SearchStatus.INFEASIBLE


@pytest.mark.parametrize("budget", [0.001, 0.2])
def test_large_trap_pool_times_out_and_never_claims_proof(budget):
    pool = _trap_pool(40)
    res = find_one(pool, pool.mode, budget_s=budget)
    assert res.status is FeasibleStatus.TIMEOUT
    assert res.lineup is None


def test_zero_budget_is_timeout():
    pool = read_salary(mini_pair("classic")[0])
    assert find_one(pool, pool.mode, budget_s=0.0).status is FeasibleStatus.TIMEOUT


@pytest.mark.parametrize("pool", [classic_pool(teams=("AAA",)), classic_pool(teams=("AAA", "BBB")),
                                  showdown_pool(teams=("AAA",))], ids=["classic1", "classic2", "showdown1"])
def test_too_few_teams_is_proven_statically(pool):
    res = find_one(pool, pool.mode)
    assert res.status is FeasibleStatus.INFEASIBLE_PROVEN and res.nodes == 0


def test_short_slot_and_salary_are_proven_statically():
    pool = classic_pool(per_team={"C": 1, "LW": 1, "RW": 1, "D": 1, "G": 1})
    wide = make_pool(Mode.CLASSIC, [r for r in pool.rows if r.position != "D"])
    assert "slot D" in find_one(wide, wide.mode).detail
    rich = classic_pool(salary=6000)
    res = find_one(rich, rich.mode)
    assert res.status is FeasibleStatus.INFEASIBLE_PROVEN and "salary cap" in res.detail


def test_cheap_one_team_prefix_is_repaired_by_backtracking():
    # Cheapest skaters are all AAA; the lineup must still reach three skater teams.
    rows = [row("g", "AAA", "G", 2500)]
    rows += [row(f"a{i}", "AAA", p, 2500) for i, p in enumerate(["C"] * 4 + ["LW"] * 4 + ["D"] * 4)]
    rows += [row("b", "BBB", "C", 6000), row("c", "CCC", "D", 6000)]
    pool = make_pool(Mode.CLASSIC, rows)
    res = find_one(pool, pool.mode)
    assert res.status is FeasibleStatus.FOUND and {"b", "c"} <= set(res.lineup)


def test_showdown_locks_from_one_team_force_the_other_team():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=8)
    aaa = [p for p in pool.persons.values() if p.flex.team == "AAA"]
    locked = {0: aaa[0].cpt.role_id, 1: aaa[1].flex.role_id, 2: aaa[2].flex.role_id, 3: aaa[3].flex.role_id}
    res = find_one(pool, pool.mode, locked=locked)
    assert res.status is FeasibleStatus.FOUND
    assert all(res.lineup[i] == rid for i, rid in locked.items())
    assert "BBB" in {pool.by_role_id[res.lineup[i]].team for i in (4, 5)}


def test_all_slots_locked():
    pool = classic_pool()
    lineup = find_one(pool, pool.mode).lineup
    res = find_one(pool, pool.mode, locked=dict(enumerate(lineup)))
    assert res.status is FeasibleStatus.FOUND and res.lineup == lineup


def test_locks_win_over_exclude_and_bad_locks_are_proof_or_error():
    pool = classic_pool()
    g = next(r for r in pool.rows if r.is_goalie)
    res = find_one(pool, pool.mode, exclude=frozenset({g.role_id}), locked={8: g.role_id})
    assert res.status is FeasibleStatus.FOUND and res.lineup[8] == g.role_id
    assert find_one(pool, pool.mode, locked={7: g.role_id}).status is FeasibleStatus.INFEASIBLE_PROVEN
    with pytest.raises(ValueError, match="not a selectable row"):
        find_one(pool, pool.mode, locked={0: "nope"})


def test_exclude_everyone_on_a_team():
    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    res = find_one(pool, pool.mode, exclude=frozenset(r.role_id for r in pool.rows if r.team == "AAA"))
    assert res.status is FeasibleStatus.FOUND
    assert "AAA" not in {pool.by_role_id[x].team for x in res.lineup}


def test_duplicate_person_and_odd_eligibility():
    base = classic_pool()
    twin_a = row("t1", "AAA", "C", 2500, roster={"C", "UTIL"}, person="twin|AAA|F")
    twin_b = row("t2", "AAA", "C", 2500, roster={"W", "UTIL"}, person="twin|AAA|F")
    util_only = row("u1", "BBB", "LW", 2500, roster={"UTIL"})
    pool = make_pool(Mode.CLASSIC, [twin_a, twin_b, util_only] + base.rows)
    res = find_one(pool, pool.mode)
    assert res.status is FeasibleStatus.FOUND
    assert len({"t1", "t2"} & set(res.lineup)) == 1
    if "u1" in res.lineup:
        assert res.lineup.index("u1") == 7


def test_feasible_module_never_imports_scipy():
    code = (
        "import sys; sys.modules['scipy'] = None\n"
        "sys.path.insert(0, 'src')\n"
        "from nhl_dfs.build.feasible import find_one\n"
        "from nhl_dfs.intake.salary import read_salary\n"
        "p = read_salary('tests/fixtures/mini/classic/DKSalaries.csv')\n"
        "r = find_one(p, p.mode)\n"
        "assert r.status.value == 'FOUND', r\n"
        "assert not any(m == 'scipy' or m.startswith('scipy.') for m in sys.modules if sys.modules[m] is not None)\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "ok"


def _random_locks(rng, pool):
    slots = CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    locks = {}
    for _ in range(rng.choice([0, 0, 1, 2])):
        i = rng.randrange(len(slots))
        ok = [r for r in pool.rows if slot_accepts(slots[i], r, pool.mode)]
        if ok:
            locks[i] = rng.choice(ok).role_id
    return locks


def test_property_milp_and_feasible_agree_on_200_random_pools():
    rng = random.Random(20260928)
    outcomes = {"found": 0, "proven": 0}
    for k in range(200):
        mode = Mode.CLASSIC if k % 2 == 0 else Mode.SHOWDOWN
        pool = random_pool(rng, mode)
        locks = _random_locks(rng, pool)
        obj = {r.role_id: rng.random() for r in pool.rows}
        m = solve_lineup(pool, mode, obj, locked=locks, time_limit_s=10.0)
        f = find_one(pool, mode, locked=locks, budget_s=10.0)
        assert m.status in (SearchStatus.FEASIBLE, SearchStatus.INFEASIBLE), (k, m)
        assert f.status is not FeasibleStatus.TIMEOUT, (k, f)
        if m.status is SearchStatus.FEASIBLE:
            assert _legal(pool, m.lineup)
            assert f.status is FeasibleStatus.FOUND, (k, mode, locks, f)
            outcomes["found"] += 1
        else:
            assert f.status is FeasibleStatus.INFEASIBLE_PROVEN, (k, mode, locks, f)
            outcomes["proven"] += 1
        if f.lineup is not None:
            assert _legal(pool, f.lineup)
            assert all(f.lineup[i] == rid for i, rid in locks.items())
    assert outcomes["found"] >= 30 and outcomes["proven"] >= 30, outcomes
