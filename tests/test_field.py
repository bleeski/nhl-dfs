import copy

import pytest

from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.models import field, ownership
from nhl_dfs.models.projection import PriorProjection
from pool_builder import varied_pool

pytestmark = pytest.mark.c3

N = 120


@pytest.fixture(scope="module")
def cfg():
    return ownership.load_ownership_config()


def build(mode, cfg, *, seed=11, n=N, family="large_gpp", time_limit_s=None, pool_seed=0):
    pool = varied_pool(mode, seed=pool_seed)
    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, cfg=cfg)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(cfg, family))
    fld = field.sample(pool, mode, util, field.behaviors_for(family, cfg), n, seed, family, proj=proj,
                       feats=feats, cfg=cfg, time_limit_s=time_limit_s)
    return pool, fld


@pytest.fixture(scope="module")
def classic(cfg):
    return build(Mode.CLASSIC, cfg)


@pytest.fixture(scope="module")
def showdown(cfg):
    return build(Mode.SHOWDOWN, cfg)


def test_classic_mass_900_goalie_100(classic):
    pool, fld = classic
    assert fld.n == N and not fld.degraded
    mass = field.marginals(fld, pool, 1000).mass(Mode.CLASSIC, pool)
    assert mass["total"] == pytest.approx(900.0, abs=1e-6)
    assert mass["goalie"] == pytest.approx(100.0, abs=1e-6)
    assert mass["skater"] == pytest.approx(800.0, abs=1e-6)


def test_showdown_mass_cpt_100_flex_500(showdown):
    pool, fld = showdown
    m = field.marginals(fld, pool, 1000)
    mass = m.mass(Mode.SHOWDOWN, pool)
    assert mass["cpt"] == pytest.approx(100.0, abs=1e-6) and mass["flex"] == pytest.approx(500.0, abs=1e-6)
    assert sum(m.cpt_share.values()) == pytest.approx(100.0)
    # CPT and FLEX never both for one person in one lineup, so person ownership <= 100%.
    assert max(m.person_own.values()) <= 100.0 + 1e-9


@pytest.mark.parametrize("which", ["classic", "showdown"])
def test_every_sampled_lineup_is_legal(which, request):
    pool, fld = request.getfixturevalue(which)
    for lu in fld.lineups:
        res = check_lineup([pool.by_role_id[r] for r in lu], pool.mode)
        assert res.ok, res.reasons


@pytest.mark.parametrize("which", ["classic", "showdown"])
def test_field_repeats_and_dup_counts_scale_to_field_size(which, request):
    pool, fld = request.getfixturevalue(which)
    m = field.marginals(fld, pool, 5000)
    assert m.repeats > 0, "sampling with replacement must produce repeated lineups"
    assert max(fld.multiplicity) > 1
    assert sum(m.dup_counts.values()) == pytest.approx(5000.0)
    k = max(m.dup_counts, key=m.dup_counts.get)
    assert m.dup_counts[k] == pytest.approx(fld.keys.count(k) / fld.n * 5000)
    m2 = field.marginals(fld, pool, 50)
    assert m2.dup_counts[k] == pytest.approx(m.dup_counts[k] / 100)


@pytest.mark.parametrize("mode", list(Mode))
def test_marginals_reproducible_under_seed(mode, cfg):
    p1, a = build(mode, cfg, seed=5, n=60)
    _, b = build(mode, cfg, seed=5, n=60)
    assert a.lineups == b.lineups and a.behavior_id == b.behavior_id
    ma, mb = field.marginals(a, p1, 1000), field.marginals(b, p1, 1000)
    assert ma.own == mb.own and ma.dup_counts == mb.dup_counts
    _, c = build(mode, cfg, seed=6, n=60)
    assert c.lineups != a.lineups


def test_behavior_split_is_largest_remainder(cfg):
    assert field.split_counts([0.3, 0.25, 0.15, 0.2, 0.1], 7) == [2, 2, 1, 1, 1]
    assert sum(field.split_counts([0.45, 0.1, 0.25, 0.15, 0.05], 301)) == 301
    _, fld = build(Mode.CLASSIC, cfg, seed=3, n=40)
    want = dict(zip([b.name for b in field.behaviors_for("large_gpp", cfg)],
                    field.split_counts([b.weight for b in field.behaviors_for("large_gpp", cfg)], 40)))
    got = {k: fld.behavior_id.count(k) for k in want}
    assert got == want


def test_stack_builders_stack(classic):
    pool, fld = classic
    from collections import Counter

    for lu, b in zip(fld.lineups, fld.behavior_id):
        if b != "stacker":
            continue
        teams = Counter(pool.by_role_id[r].team for r in lu if not pool.by_role_id[r].is_goalie)
        assert max(teams.values()) >= 3


def test_short_draw_is_degraded_and_rates_use_actual_draws(cfg):
    pool, fld = build(Mode.CLASSIC, cfg, n=30, time_limit_s=0.0)
    assert fld.n < 30 and fld.degraded and fld.detail
    m = field.marginals(fld, pool, 1000)
    assert m.degraded and m.n_draws == fld.n
    if fld.n == 0:
        assert all(v == 0.0 for v in m.own.values()) and m.dup_counts == {}


def test_dup_proxy_monotone_in_ownership_including_zero_owned(cfg):
    lineup = ("a", "b", "c")
    floor = cfg["dup_proxy"]["own_floor_pct"]
    prev = None
    for own_a in (0.0, floor / 2, floor, 1.0, 5.0, 20.0, 60.0, 100.0):
        v = field.dup_proxy(lineup, {"a": own_a, "b": 10.0}, Mode.CLASSIC, salary_left=500, cfg=cfg)  # c never drawn
        assert v == v and v != float("-inf")
        if prev is not None:
            assert v >= prev
            if own_a > floor:
                assert v > prev
        prev = v


def test_dup_proxy_salary_left_and_captain(cfg):
    own = {"x": 30.0, "y": 10.0}
    at_cap = field.dup_proxy(("x", "y"), own, Mode.CLASSIC, salary_left=0, cfg=cfg)
    loose = field.dup_proxy(("x", "y"), own, Mode.CLASSIC, salary_left=3000, cfg=cfg)
    assert at_cap > loose
    sd = field.dup_proxy(("x", "y"), own, Mode.SHOWDOWN, salary_left=0, cfg=cfg)
    assert sd == pytest.approx(at_cap + cfg["dup_proxy"]["captain_weight"] * ownership.log_floor(30.0, 0.1))


def test_unknown_family_uses_large_gpp_mixture(cfg):
    assert [b.weight for b in field.behaviors_for("no_such_family", cfg)] == \
        [b.weight for b in field.behaviors_for("large_gpp", cfg)]
    c = copy.deepcopy(cfg)
    c["field"]["mixtures"]["cash"] = {"optimizer": 1.0}
    assert [b.name for b in field.behaviors_for("cash", c)] == ["optimizer"]
