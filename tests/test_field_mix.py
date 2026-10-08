"""C19: the field's stack mix. team4 (4 skaters of one team) and double_stack (4 of one team and 3 of another)
opponents in both samplers, the same numbers for the old rules, and (later commits) the Classic mixtures and the
shape line. Everything here runs without a network and without any gitignored file; the one test that needs the
2026-09-30 pool reads the path in NHL_DFS_C19_POOL and skips loudly when it is absent."""

import hashlib
import math
import re
from collections import Counter

import numpy as np
import pytest

from nhl_dfs.build.milp import GroupConstraint, LineupModel
from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.models import field as fm
from nhl_dfs.models import field_fast as ff
from nhl_dfs.models import ownership
from nhl_dfs.models.projection import PriorProjection
from pool_builder import stand_in_pool, varied_pool

pytestmark = pytest.mark.c19

CAP = 50_000


def skater_shape(pool, lineup) -> tuple[int, ...]:
    """Skaters per team, largest first, counted here from the role ids (never from the sampler's own stack_freq)."""
    c = Counter()
    for rid in lineup:
        row = pool.by_role_id[rid]
        if not row.is_goalie:
            c[row.team] += 1
    return tuple(sorted(c.values(), reverse=True))


def setup_for(pool, family="large_gpp"):
    proj = PriorProjection(pool)
    cfg = ownership.load_ownership_config()
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, family))
    return proj, cfg, feats, util


def beh(name, rule, weight=1.0, noise=3.0):
    return fm.Behavior(name, weight, noise, rule, "projection", 0.0, 1.0)


@pytest.fixture(scope="module")
def big():
    pool = stand_in_pool(6)
    return (pool, *setup_for(pool))


@pytest.fixture(scope="module")
def small():
    pool = varied_pool(Mode.CLASSIC, seed=3)
    return (pool, *setup_for(pool))


def legal(pool, lu) -> bool:
    rows = [pool.by_role_id[r] for r in lu]
    return check_lineup(rows, Mode.CLASSIC).ok and sum(r.salary for r in rows) <= CAP


def fast(setup, behaviors, n, seed=7, **kw):
    pool, proj, cfg, feats, util = setup
    return ff.sample_fast(pool, util, behaviors, n, seed, "large_gpp", proj=proj, feats=feats, cfg=cfg, **kw)


# -- the new rules, vectorized sampler -----------------------------------------------------------------

def test_team4_draws_have_four_skaters_of_one_team(big):
    pool = big[0]
    f = fast(big, [beh("stacker4", "team4")], 400)
    assert f.n == 400 and all(b == "stacker4" for b in f.behavior_id)
    for lu in f.lineups:
        assert legal(pool, lu)
        assert skater_shape(pool, lu)[0] >= 4


def test_double_stack_draws_are_four_three_one(big):
    pool = big[0]
    f = fast(big, [beh("double_stack", "double_stack")], 400)
    assert f.n == 400
    for lu in f.lineups:
        assert legal(pool, lu)
        s = skater_shape(pool, lu)
        assert s[0] >= 4 and s[1] >= 3  # the third-team rule leaves room for one more skater only
        assert s == (4, 3, 1)


def test_double_stack_spreads_over_many_pairs(big):
    pool = big[0]
    f = fast(big, [beh("double_stack", "double_stack")], 600)
    pairs = {tuple(sorted({pool.by_role_id[r].team for r in lu if not pool.by_role_id[r].is_goalie},
                          key=lambda t: -sum(pool.by_role_id[r].team == t for r in lu)))[:2] for lu in f.lineups}
    assert len(pairs) >= 20  # 30 ordered pairs on 6 teams, 600 draws


def test_team3_draws_still_exist_in_a_mixture(big):
    pool = big[0]
    mix = [beh("stacker", "team3", 0.3), beh("stacker4", "team4", 0.3), beh("double_stack", "double_stack", 0.3),
           beh("optimizer", "none", 0.1, 0.75)]
    f = fast(big, mix, 500)
    want = dict(zip([b.name for b in mix], fm.split_counts([b.weight for b in mix], 500)))
    assert {k: f.behavior_id.count(k) for k in want} == want
    t3 = [lu for lu, b in zip(f.lineups, f.behavior_id) if b == "stacker"]
    assert t3 and all(skater_shape(pool, lu)[0] >= 3 for lu in t3)
    assert any(skater_shape(pool, lu)[0] == 3 for lu in t3)  # a 3-stack stays a 3-stack, not always a bigger one


def test_fallbacks_are_few_and_nothing_is_dropped(big):
    for rule in ("team4", "double_stack"):
        f = fast(big, [beh(rule, rule)], 600)
        assert f.n == 600, f.detail
        text = " ".join(f.detail)
        assert "dropped" not in text
        m = re.search(r"(\d+) draw\(s\) failed a check", text)
        assert m is None or int(m.group(1)) <= 6  # at most 1% of the draws went to the MILP


def test_same_seed_gives_the_same_vectorized_field(big):
    mix = [beh("stacker4", "team4", 0.5), beh("double_stack", "double_stack", 0.5)]
    a = fast(big, mix, 300, milp_fallback=False)
    b = fast(big, mix, 300, milp_fallback=False)
    assert a.lineups == b.lineups and a.behavior_id == b.behavior_id
    c = fast(big, mix, 300, seed=8, milp_fallback=False)
    assert c.lineups != a.lineups


# -- the forcing itself ----------------------------------------------------------------------------------

def forced_counts(A, forced):
    cols = np.nonzero(forced[0])[0]
    return [int((A.grp[cols] == g).sum()) for g in (ff.C, ff.W, ff.D)]


def test_forcing_never_makes_an_uncompletable_set():
    pool = stand_in_pool(6)
    A = ff.ClassicArrays(pool)
    cols0 = A.skaters[A.team[A.skaters] == 0]
    cols1 = A.skaters[A.team[A.skaters] == 1]
    # four centers rank first by value: at most 3 can ever be forced (C, C and the UTIL)
    U = np.zeros((1, A.R))
    U[0, cols0[A.grp[cols0] == ff.C][:4]] = 100.0 - np.arange(4)
    U[0, cols0[A.grp[cols0] == ff.W][:2]] = 10.0
    forced, block = ff._force(A, U, np.array([0]), ff.StackSpec(4))
    c, w, d = forced_counts(A, forced)
    assert block is None and forced.sum() == 4 and forced[0, cols0].sum() == 4
    assert c <= 3 and max(c, 2) + max(w, 3) + max(d, 2) <= 8
    # team 0's best are 2 C + 2 W and team 1's best three are C, W, W: together 3 C + 4 W, which cannot be completed
    U = np.zeros((1, A.R))
    for cols, (nc, nw) in ((cols0, (2, 2)), (cols1, (1, 2))):
        U[0, cols[A.grp[cols] == ff.C][:nc]] = 100.0
        U[0, cols[A.grp[cols] == ff.W][:nw]] = 90.0
    forced, block = ff._force(A, U, np.array([0]), ff.StackSpec(4, np.array([1]), 3))
    c, w, d = forced_counts(A, forced)
    assert forced.sum() == 7 and max(c, 2) + max(w, 3) + max(d, 2) <= 8
    assert forced[0, cols0].sum() == 4 and forced[0, cols1].sum() == 3


def test_a_double_stack_blocks_both_teams_from_the_fill(big):
    pool = big[0]
    A = ff.ClassicArrays(pool)
    st, st2 = np.array([0, 2]), np.array([1, 3])
    V = np.zeros((2, A.R)) + A.sal[None, :] / 1000.0
    spec = ff.StackSpec(4, st2, 3)
    forced, block = ff._force(A, V, st, spec)
    for i in range(2):
        assert forced[i].sum() == 7 and block[i, A.skaters][A.team[A.skaters] == st[i]].all()
        assert not block[i, A.idx[ff.G]].any()


# -- old rules unchanged -----------------------------------------------------------------------------------

GOLDEN_SHA256 = "a11f56b112ff9be6e84a0a7f97a1c95a2a216a5fb35d3512503bf3277665e754"  # origin/master 2012793, flag 44


def test_old_rules_give_the_same_vectorized_lineups_as_before_c19():
    """Flag 44: varied_pool(CLASSIC, seed=3), n=300, seed 11, large_gpp, the old mixture, MILP fallback off.
    The expected hash was computed on a clean export of origin/master 2012793 before any C19 edit. If numpy changes
    its Generator stream this test fails for that reason alone: say so, do not edit the hash to match new code."""
    pool = varied_pool(Mode.CLASSIC, seed=3)
    proj, cfg, feats, util = setup_for(pool)
    f = ff.sample_fast(pool, util, fm.behaviors_for("large_gpp", cfg), 300, 11, "large_gpp", proj=proj, feats=feats,
                       cfg=cfg, milp_fallback=False)
    h = hashlib.sha256()
    for lu, b in zip(f.lineups, f.behavior_id):
        h.update((",".join(lu) + "|" + b + "\n").encode())
    assert f.n == 298
    assert h.hexdigest() == GOLDEN_SHA256


def _old_jobs(b_rule, n_b, mode, pool, skaters_by_team, team_total):
    """The job logic of models.field.sample before C19, copied here as the reference: (team or None, draws)."""
    stack_teams = sorted(t for t, rids in skaters_by_team.items()
                         if len({pool.by_role_id[x].person_key for x in rids}) >= fm._stack_min(mode))
    if b_rule == "team3" and stack_teams:
        per_team = fm.split_counts([math.exp(team_total[t]) for t in stack_teams], n_b)
        return [(t, k) for t, k in zip(stack_teams, per_team) if k]
    return [(None, n_b)]


@pytest.mark.parametrize("n", [1, 7, 97, 300])
def test_none_and_team3_jobs_equal_the_old_logic(n):
    for pool in (stand_in_pool(6), varied_pool(Mode.CLASSIC, seed=1)):
        sk: dict[str, list[str]] = {}
        for r in pool.rows:
            if not r.is_goalie:
                sk.setdefault(r.team, []).append(r.role_id)
        totals = {t: 0.31 * i - 0.5 for i, t in enumerate(sorted(sk))}
        for rule in ("none", "team3"):
            new, note = fm.stack_jobs(rule, Mode.CLASSIC, n, pool, sk, totals)
            assert note is None
            assert [((reqs[0][0] if reqs else None), k) for reqs, k in new] == _old_jobs(rule, n, Mode.CLASSIC, pool, sk, totals)
        # Showdown keeps its 4-of-6 threshold for team3
        new, _ = fm.stack_jobs("team3", Mode.SHOWDOWN, n, pool, sk, totals)
        assert all(m == 4 for reqs, _ in new for _, m in reqs)


def test_a_rule_nothing_can_satisfy_degrades_visibly(small):
    pool = small[0]
    sk = {"AAA": ["1", "2", "3"], "BBB": ["4", "5", "6"]}
    persons = {rid: f"p{rid}" for ids in sk.values() for rid in ids}

    class P:  # just enough of a pool for the size count
        by_role_id = {rid: type("R", (), {"person_key": pk}) for rid, pk in persons.items()}

    jobs, note = fm.stack_jobs("team4", Mode.CLASSIC, 10, P, sk, {"AAA": 0.0, "BBB": 0.0})
    assert jobs == [((), 10)] and "no team has 4 skaters" in note
    big_sk = {"AAA": [str(i) for i in range(1, 6)], "BBB": [str(i) for i in range(6, 11)]}
    P.by_role_id = {rid: type("R", (), {"person_key": f"p{rid}"}) for ids in big_sk.values() for rid in ids}
    jobs, note = fm.stack_jobs("double_stack", Mode.CLASSIC, 10, P, big_sk, {"AAA": 0.0, "BBB": 0.0})
    assert jobs == [((), 10)] and "no legal 4-3 pair" in note  # two teams cannot make a 4-3-1


def test_new_rules_are_classic_only():
    pool = varied_pool(Mode.SHOWDOWN, seed=0)
    for rule in ("team4", "double_stack"):
        with pytest.raises(ValueError, match="Classic only"):
            fm.stack_jobs(rule, Mode.SHOWDOWN, 5, pool, {}, {})
    with pytest.raises(ValueError, match="unknown stack rule"):
        fm.stack_jobs("team5", Mode.CLASSIC, 5, pool, {}, {})


# -- the MILP sampler on the new rules ------------------------------------------------------------------------

@pytest.mark.parametrize("rule", ["team4", "double_stack"])
def test_milp_sampler_draws_obey_the_new_rules(rule, small):
    pool, proj, cfg, feats, util = small
    fld = fm.sample(pool, Mode.CLASSIC, util, [beh(rule, rule)], 16, 5, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    assert fld.n == 16 and not fld.degraded
    for lu in fld.lineups:
        assert legal(pool, lu)
        s = skater_shape(pool, lu)
        assert s[0] >= 4 and (rule == "team4" or s[1] >= 3)


def test_parallel_milp_sampler_obeys_the_double_stack_and_ignores_workers(small):
    pool, proj, cfg, feats, util = small
    kw = dict(proj=proj, feats=feats, cfg=cfg, sub_size=4)
    a = fm.sample_parallel(pool, Mode.CLASSIC, util, [beh("double_stack", "double_stack")], 12, 5, "large_gpp", workers=1, **kw)
    b = fm.sample_parallel(pool, Mode.CLASSIC, util, [beh("double_stack", "double_stack")], 12, 5, "large_gpp", workers=3, **kw)
    assert a.keys == b.keys and a.n == 12
    assert all(skater_shape(pool, lu) == (4, 3, 1) for lu in a.lineups)


# -- the two samplers agree on the new rules (flag 45, the C8 rule) --------------------------------------------

_AGREE: dict = {}


def agreement(rule: str, which: str):
    """Identical perturbed objectives, 40 draws, team 0 (and team 1 for a double stack): (draws the vectorized solve
    could not finish, mean relative value gap to the MILP). Asserts legality and that no vectorized lineup beats
    the MILP. Cached: the 40 MILP solves are the cost."""
    if (rule, which) in _AGREE:
        return _AGREE[(rule, which)]
    pool = stand_in_pool(6) if which == "big" else varied_pool(Mode.CLASSIC, seed=3)
    proj, cfg, feats, util = setup_for(pool)
    A = ff.ClassicArrays(pool)
    assert A.ok
    b = beh(rule, rule)
    obj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, b, cfg["field"]["captain_rules"])
    V = ff.perturbed(A, obj, 40, b.noise_sd, 3)
    double = rule == "double_stack"
    got, bad = ff.solve_batch(A, V, np.full(40, 0), ff.StackSpec(4, np.full(40, 1) if double else None, 3))
    sk = {t: frozenset(r.role_id for r in pool.rows if r.team == A.team_names[t] and not r.is_goalie) for t in (0, 1)}
    groups = (GroupConstraint(role_ids=sk[0], min_count=4),) + ((GroupConstraint(role_ids=sk[1], min_count=3),) if double else ())
    model = LineupModel(pool, Mode.CLASSIC, groups=groups)
    gaps = []
    for i in range(40):
        if got[i] is None:
            continue
        assert legal(pool, got[i])
        res = model.solve({r: float(V[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
        vm = sum(V[i, A.ids.index(r)] for r in res.lineup)
        vf = sum(V[i, A.ids.index(r)] for r in got[i])
        assert vf <= vm + 1e-6  # a legal lineup is never better than the optimum
        gaps.append((vm - vf) / abs(vm))
    _AGREE[(rule, which)] = (bad, float(np.mean(gaps)))
    return _AGREE[(rule, which)]


@pytest.mark.parametrize("which", ["small", "big"])
@pytest.mark.parametrize("rule", ["team4", "double_stack"])
def test_vectorized_lineups_are_legal_and_rarely_need_the_milp(rule, which):
    bad, _ = agreement(rule, which)
    assert bad <= 2


@pytest.mark.parametrize("which", ["small", "big"])
@pytest.mark.parametrize("rule", ["team4", "double_stack"])
def test_mean_value_gap_to_the_milp_is_under_one_percent(rule, which):
    """Flag 45 as written: noise seed 3, team 0 (and team 1), 40 draws. The seed was fixed before the first run; the
    forcing was then changed (flag 45 outcome) after the first version measured 1.08 to 2.19 percent. Other seeds are
    in the session log, not here: on fresh seeds double_stack reached 1.05 and 1.25 percent in 2 of 12 cases."""
    assert agreement(rule, which)[1] < 0.01
