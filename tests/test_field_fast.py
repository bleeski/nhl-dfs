"""C8 vectorized Classic field sampler: always legal, deterministic, and close to the MILP optimum."""

import numpy as np
import pytest

from nhl_dfs.build.milp import GroupConstraint, LineupModel
from nhl_dfs.contracts.geometry import Mode, check_lineup, lineup_key
from nhl_dfs.models import field as fm
from nhl_dfs.models import field_fast as ff
from nhl_dfs.models import ownership
from nhl_dfs.models.projection import PriorProjection
from pool_builder import varied_pool

pytestmark = pytest.mark.c8


@pytest.fixture(scope="module")
def setup():
    pool = varied_pool(Mode.CLASSIC, seed=3)
    proj = PriorProjection(pool)
    cfg = ownership.load_ownership_config()
    feats = ownership.feature_table(pool, proj, None, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, "large_gpp"))
    return pool, proj, cfg, feats, util


def test_every_lineup_is_legal_and_the_draws_are_deterministic(setup):
    pool, proj, cfg, feats, util = setup
    beh = fm.behaviors_for("large_gpp", cfg)
    a = ff.sample_fast(pool, util, beh, 300, 11, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    b = ff.sample_fast(pool, util, beh, 300, 11, "large_gpp", proj=proj, feats=feats, cfg=cfg)
    assert a is not None and a.n == 300 and a.keys == b.keys
    for lu in a.lineups:
        assert check_lineup([pool.by_role_id[r] for r in lu], Mode.CLASSIC).ok
    stack = [lu for lu, name in zip(a.lineups, a.behavior_id) if name == "stacker"]
    for lu in stack:  # the stacking behavior keeps >= 3 skaters of one team
        teams = [pool.by_role_id[r].team for r in lu if not pool.by_role_id[r].is_goalie]
        assert max(teams.count(t) for t in set(teams)) >= 3


def test_close_to_the_milp_on_identical_objectives(setup):
    pool, proj, cfg, feats, util = setup
    A = ff.ClassicArrays(pool)
    assert A.ok
    for b in fm.behaviors_for("large_gpp", cfg):
        obj = fm.behavior_objective(pool, Mode.CLASSIC, util, proj, feats, b, cfg["field"]["captain_rules"])
        V = ff.perturbed(A, obj, 40, b.noise_sd, 3)
        team = A.team_names[0] if b.stack_rule == "team3" else None
        got, bad = ff.solve_batch(A, V, np.full(40, A.team_names.index(team) if team else -1))
        groups = (GroupConstraint(role_ids=frozenset(r.role_id for r in pool.rows if r.team == team and not r.is_goalie),
                                  min_count=3),) if team else ()
        model = LineupModel(pool, Mode.CLASSIC, groups=groups)
        gaps = []
        for i in range(40):
            if got[i] is None:
                continue
            res = model.solve({r: float(V[i, c]) for c, r in enumerate(A.ids)}, time_limit_s=5.0)
            vm = sum(V[i, A.ids.index(r)] for r in res.lineup)
            vf = sum(V[i, A.ids.index(r)] for r in got[i])
            assert vf <= vm + 1e-6  # never better than the optimum: it is a legal lineup
            gaps.append((vm - vf) / abs(vm))
        assert bad <= 2 and np.mean(gaps) < 0.01, (b.name, bad, np.mean(gaps))


def test_non_compact_pools_return_none():
    from pool_builder import random_pool
    import random

    for s in range(20):
        pool = random_pool(random.Random(s), Mode.CLASSIC)
        if not ff.ClassicArrays(pool).ok:
            assert ff.sample_fast(pool, {}, [], 10, 1, "x", proj=PriorProjection(pool)) is None
            return
    pytest.skip("no non-compact random pool in 20 seeds")
