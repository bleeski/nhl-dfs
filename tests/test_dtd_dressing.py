"""C24 (backlog B14, flags 101 to 106): a DTD skater's participation is drawn inside the simulator's dressing step, once,
from his play probability, and when he sits his ice time, shots and blocks go to the teammates who dressed.

Synthetic slates, no history, no network. The DTD skater below is a top-line, PP1 forward whose dressing probability is set
to 1, so "he did not dress" means exactly "the DTD draw sat him" and the comparisons isolate the new draw. Assertions that a
sitting skater scores nothing allow shootout-goal multiples (backlog B29: the shootout can still credit an undressed skater).
"""

import hashlib

import numpy as np
import pytest

from nhl_dfs.sim import game, score
from nhl_dfs.sim import outcomes as oc
from sim_helpers import slate_for, synthetic_params

pytestmark = pytest.mark.c24

TEAMS = ("AAA", "BBB", "CCC", "DDD")
GAMES = (("AAA", "BBB"), ("CCC", "DDD"))
DTD = "AAA-F00|AAA|F"
MATES = ("AAA-F01|AAA|F", "AAA-F02|AAA|F")  # his line (L0) and PP1 unit
N = 20000


def _params():
    p = synthetic_params(TEAMS)
    p.persons[DTD].opportunity.p_dress = 1.0
    return p


def _sim(params, n, participation=None, seed=11, purpose="selection"):
    slate = slate_for(params, GAMES)
    slate.participation = dict(participation or {})
    return oc.concat(list(game.iter_chunks(slate, params, n, seed, purpose)))


@pytest.fixture(scope="module")
def world():
    params = _params()
    return params, _sim(params, N), _sim(params, N, {DTD: 0.85})


def _col(o, key):
    return o.person_keys.index(key)


def _se(*xs):
    return float(np.sqrt(sum(np.var(x, ddof=1) / len(x) for x in xs)))


# -- reproducibility (flag 104) ------------------------------------------------------------------------------------------

GOLDEN_SHA256 = "1eb36ca6567348c9fb0598305817bbeda59e991a1dd1d4b2d56918bd28aceed7"  # origin/master 54c6640, flag 104


def test_a_slate_with_no_dtd_skater_draws_exactly_what_origin_master_draws():
    """Computed on a clean export of origin/master 54c6640 (its src is that of 1005933) before any C24 edit. If numpy
    changes its Generator stream this test fails for that reason alone: say so, do not edit the hash to match new code."""
    params = synthetic_params(TEAMS)
    slate = slate_for(params, GAMES)
    h = hashlib.sha256()
    for o in game.iter_chunks(slate, params, 2500, 11, "selection"):
        for name in oc.PERSON_2D + oc.TEAM_2D + oc.GAME_2D:
            h.update(name.encode() + getattr(o, name).tobytes())
    assert h.hexdigest() == GOLDEN_SHA256


def test_a_dtd_skater_changes_only_his_own_game(world):
    params, base, dtd = world
    other = [j for j, k in enumerate(base.person_keys) if params.persons[k].team in ("CCC", "DDD")]
    mine = [j for j, k in enumerate(base.person_keys) if params.persons[k].team in ("AAA", "BBB")]
    for name in oc.PERSON_2D:
        assert np.array_equal(getattr(base, name)[:, other], getattr(dtd, name)[:, other]), name
    t = [base.teams.index("CCC"), base.teams.index("DDD")]
    for name in oc.TEAM_2D:
        assert np.array_equal(getattr(base, name)[:, t], getattr(dtd, name)[:, t]), name
    g = base.games.index("DDD@CCC")
    for name in oc.GAME_2D:
        assert np.array_equal(getattr(base, name)[:, g], getattr(dtd, name)[:, g]), name
    assert not np.array_equal(base.sog[:, mine], dtd.sog[:, mine])


def test_play_probability_one_or_zero_is_never_drawn_inside_the_game():
    """OUT (0) and PLAYING (1) are not the draw's: the simulation is byte-identical to no participation at all (an OUT
    person keeps today's mask, build/objectives.split_play)."""
    params = synthetic_params(("AAA", "BBB"))
    a = _sim_one(params, {})
    b = _sim_one(params, {DTD: 1.0, "AAA-F05|AAA|F": 0.0, "BBB-G0|BBB|G": 0.5})
    for name in oc.PERSON_2D + oc.TEAM_2D:
        assert np.array_equal(getattr(a, name), getattr(b, name)), name


def _sim_one(params, participation, n=3000):
    slate = slate_for(params)
    slate.participation = dict(participation)
    return oc.concat(list(game.iter_chunks(slate, params, n, 7, "referee")))


def test_his_draw_has_its_own_stream_and_does_not_depend_on_who_else_is_covered():
    params = _params()
    other = "AAA-D03|AAA|D"
    a = _sim_one(params, {DTD: 0.85})
    b = _sim_one(params, {DTD: 0.85, other: 0.5})
    j = _col(a, DTD)
    assert np.array_equal(a.dressed[:, j], b.dressed[:, j])  # he sits in exactly the same scenarios
    assert not np.array_equal(a.dressed[:, _col(a, other)], b.dressed[:, _col(b, other)])


# -- the DTD skater himself (the card: his mean is p_play times his dressed mean) -------------------------------------------

def test_his_mean_is_p_play_times_his_dressed_mean_not_squared(world):
    params, base, dtd = world
    j = _col(base, DTD)
    share = float(dtd.dressed[:, j].mean())
    se_share = np.sqrt(0.85 * 0.15 / N)
    assert abs(share - 0.85) <= 4 * se_share
    assert abs(share - 0.85 ** 2) > 20 * se_share  # a mask stacked on the draw would show 0.7225
    p0, p1 = score.base_tenths(base)[:, j].astype(float), score.base_tenths(dtd)[:, j].astype(float)
    assert abs(p1.mean() - 0.85 * p0.mean()) <= 4 * _se(0.85 * p0, p1)
    sat = ~dtd.dressed[:, j]
    from nhl_dfs.contracts.scoring import SHOOTOUT_GOAL_TENTHS

    assert not (score.base_tenths(dtd)[sat, j] % SHOOTOUT_GOAL_TENTHS).any()  # nothing but a B29 shootout goal when he sits
    assert not (dtd.sog[sat, j].any() or dtd.blocks[sat, j].any() or dtd.goals[sat, j].any() or dtd.assists[sat, j].any())


# -- conservation (the card: team minutes and shots are conserved when he sits) -----------------------------------------------

def test_the_groups_ice_time_is_conserved_exactly_in_every_scenario():
    """Flag 103's quantity: per scenario and group, the sum of dm x (EV + PP + SH seconds) over the dressed skaters,
    phantom included, is what it would have been with him in. Shots and blocks: the same identity on their means."""
    params = _params()
    slate = slate_for(params)
    slate.participation = {DTD: 0.85, "AAA-D00|AAA|D": 0.5}
    prep = game.prepare(slate, params)
    tp = prep.teams["AAA"]
    n = 4000
    dressed, dm = game._presence(np.random.Generator(np.random.PCG64(5)), tp, n, slate.cfg)
    plays = game._plays(tp, n, 11, 1, 0, 0)
    keep, dm_toi, dm_sog, dm_blk = game._sit_out(tp, dressed, dm, plays)
    sits = dressed & ~keep
    assert sits[:, tp.part_idx].any(axis=0).all() and not sits[:, np.setdiff1d(np.arange(tp.scale.size), tp.part_idx)].any()
    toi = tp.toi.sum(axis=1)
    for grp in (tp.is_f, ~tp.is_f):
        for after, amount in ((dm_toi, toi), (dm_sog, tp.saved), (dm_blk, tp.blk)):
            np.testing.assert_allclose(after[:, grp] @ amount[grp], dm[:, grp] @ amount[grp], rtol=1e-12)
    assert not dm_toi[sits].any() and not dm_sog[sits].any() and not dm_blk[sits].any()
    calm = ~sits.any(axis=1)
    assert np.array_equal(dm_toi[calm], dm[calm]) and np.array_equal(dm_sog[calm], dm[calm])
    jf = int(tp.part_idx[tp.is_f[tp.part_idx]][0])
    jd = int(tp.part_idx[~tp.is_f[tp.part_idx]][0])
    rows = sits[:, jf] & ~sits[:, jd]  # only the forward sat: the defense is untouched, every other dressed forward gains
    assert rows.any() and np.array_equal(dm_toi[rows][:, ~tp.is_f], dm[rows][:, ~tp.is_f])
    others = tp.is_f.copy()
    others[jf] = False
    d0, d1 = dm[rows][:, others], dm_toi[rows][:, others]
    assert ((d1 > d0) | (d0 == 0)).all() and (d1 > d0).any()


def test_team_shots_and_blocks_hold_when_he_sits_and_his_linemates_rise(world):
    params, base, dtd = world
    j = _col(dtd, DTD)
    sat = ~dtd.dressed[:, j]
    ti = dtd.teams.index("AAA")
    aaa = [i for i, k in enumerate(dtd.person_keys) if params.persons[k].team == "AAA" and params.persons[k].group != "G"]
    sog = dtd.team_sog[:, ti].astype(float)
    blk = dtd.blocks[:, aaa].sum(axis=1).astype(float)
    for x in (sog, blk):
        assert abs(x[sat].mean() - x[~sat].mean()) <= 4 * _se(x[sat], x[~sat])
    assert abs(sog.mean() - base.team_sog[:, ti].mean()) <= 4 * _se(sog, base.team_sog[:, ti].astype(float))
    pts = score.base_tenths(dtd).astype(float)
    for m in MATES:
        c = _col(dtd, m)
        assert pts[sat, c].mean() - pts[~sat, c].mean() > 3 * _se(pts[sat, c], pts[~sat, c]), m
        assert dtd.sog[sat, c].mean() > dtd.sog[~sat, c].mean()
    # the mask world (no reallocation) has no such lift: his linemates are indifferent to scenarios he would have been masked in
    pts0 = score.base_tenths(base).astype(float)
    c = _col(base, MATES[0])
    assert abs(pts0[sat, c].mean() - pts0[~sat, c].mean()) <= 4 * _se(pts0[sat, c], pts0[~sat, c])


def test_goalies_keep_their_start_logic_and_the_opposing_goalie_faces_the_same_shots(world):
    params, base, dtd = world
    sa, sb = slate_for(params, GAMES), slate_for(params, GAMES)
    sb.participation = {DTD: 0.85}
    pa, pb = game.prepare(sa, params), game.prepare(sb, params)
    for t in TEAMS:
        ga, gb = pa.teams[t].goalies, pb.teams[t].goalies
        assert np.array_equal(ga.cols, gb.cols) and np.array_equal(ga.p_start, gb.p_start)
        assert np.array_equal(ga.pull_c, gb.pull_c) and ga.unlisted == gb.unlisted
        assert not pb.teams[t].part_idx.size or t == "AAA"
    for k, pp in params.persons.items():
        if pp.group == "G" and pp.team in ("AAA", "BBB"):
            p = float(pp.goalie.p_start)
            share = float(dtd.started[:, _col(dtd, k)].mean())
            assert abs(share - p) <= 4 * np.sqrt(max(p * (1 - p), 1e-9) / N), k
    bbb = [_col(dtd, k) for k, pp in params.persons.items() if pp.group == "G" and pp.team == "BBB"]
    s1, s0 = dtd.saves[:, bbb].sum(axis=1).astype(float), base.saves[:, bbb].sum(axis=1).astype(float)
    assert abs(s1.mean() - s0.mean()) <= 4 * _se(s1, s0)


def test_a_dtd_skater_changes_the_spec_hash_and_nothing_else_does():
    from nhl_dfs.sim import cache as cache_mod

    params = synthetic_params(("AAA", "BBB"))
    a, b = slate_for(params), slate_for(params)
    assert cache_mod.spec_hash(a, params, 1, "selection", 100) == cache_mod.spec_hash(b, params, 1, "selection", 100)
    b.participation = {DTD: 0.85}
    assert cache_mod.spec_hash(a, params, 1, "selection", 100) != cache_mod.spec_hash(b, params, 1, "selection", 100)


def test_the_assist_pilot_never_sees_the_draw():
    params = _params()
    sa, sb = slate_for(params), slate_for(params)
    sb.participation = {DTD: 0.85}
    pa, pb = game.prepare(sa, params), game.prepare(sb, params)
    assert np.array_equal(pa.teams["AAA"].a_mult, pb.teams["AAA"].a_mult)
    assert np.array_equal(pa.teams["AAA"].scale, pb.teams["AAA"].scale)  # the phantom budget too
