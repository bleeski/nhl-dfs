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


# -- the split and the mask (flags 101, 102) ---------------------------------------------------------------------------------

def test_the_split_covers_skaters_strictly_between_zero_and_one():
    from nhl_dfs.build import objectives as ob

    params = synthetic_params(("AAA", "BBB"))
    play = {DTD: 0.85, "AAA-D01|AAA|D": 0.5, "AAA-F05|AAA|F": 0.0, "AAA-G0|AAA|G": 0.85, "nobody|AAA|F": 0.85}
    in_sim, mask = ob.split_play(play, params)
    assert in_sim == {DTD: 0.85, "AAA-D01|AAA|D": 0.5}
    assert mask == {"AAA-F05|AAA|F": 0.0, "AAA-G0|AAA|G": 0.85, "nobody|AAA|F": 0.85}  # OUT, a goalie, outside the table
    assert ob.split_play(None, params) == ({}, {}) and ob.split_play({}, params) == ({}, {})


def test_an_uncovered_persons_mask_is_byte_identical_to_the_mask_before_c24():
    """The covered skater's mask draws are still consumed (and discarded), so a goalie sorted after him keeps exactly the
    mask origin/master drew for him with the whole dictionary."""
    from nhl_dfs.build import objectives as ob

    keys = ["a|AAA|F", "b|AAA|G", "c|AAA|D"]
    play = {"a|AAA|F": 0.85, "b|AAA|G": 0.5, "c|AAA|D": 0.0}
    before = ob.play_mask(5000, keys, play, seed=4, purpose_code=1)
    after = ob.play_mask(5000, keys, play, seed=4, purpose_code=1, in_sim={"a|AAA|F"})
    assert after[:, 0].all() and np.array_equal(after[:, 1:], before[:, 1:])
    assert not before[:, 0].all() and not after[:, 2].any()  # OUT stays out


def test_showdown_captain_and_flex_rows_share_one_draw(world):
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.contracts.geometry import Mode
    from pool_builder import make_pool, row

    params, base, dtd = world
    keys = sorted(params.persons)
    rows = []
    for i, k in enumerate(keys):
        pos = {"F": "C", "D": "D", "G": "G"}[params.persons[k].group]
        rows.append(row(f"{i}c", params.persons[k].team, pos, 6000, roster={"CPT"}, person=k, mode=Mode.SHOWDOWN))
        rows.append(row(f"{i}f", params.persons[k].team, pos, 4000, roster={"FLEX"}, person=k, mode=Mode.SHOWDOWN))
    pool = make_pool(Mode.SHOWDOWN, rows)
    s = ob.scenario_set(score.base_tenths(dtd), keys, pool, purpose="selection", seed=11, purpose_code=1,
                        play_prob={DTD: 0.85}, in_sim={DTD})
    i = keys.index(DTD)
    cpt, flex = s.col[f"{i}c"], s.col[f"{i}f"]
    assert np.array_equal(s.base[:, cpt], s.base[:, flex])
    assert np.array_equal(s.base[:, cpt], score.base_tenths(dtd)[:, _col(dtd, DTD)])  # the draws as simulated, never masked
    assert "drawn inside the simulated game for 1 skater(s)" in s.notes[0] and len(s.notes) == 1


# -- records (flag 105) -------------------------------------------------------------------------------------------------------

def test_the_participation_record_is_format_2_and_settle_labels_it():
    from nhl_dfs.build import scenario_cache as sc
    from nhl_dfs.learn import grade_forecasts as gf

    params = synthetic_params(("AAA", "BBB"))
    g = "AAA-G0|AAA|G"
    rec = sc.participation_record(params, {DTD: 0.85, g: 0.5}, None, in_sim={DTD: 0.85})
    p = rec["persons"]
    p_dress = params.persons[DTD].opportunity.p_dress
    assert rec["format"] == 2 and p[DTD]["draw"] == 0.85 and p[DTD]["mask"] == 1.0
    assert p[DTD]["p_play"] == pytest.approx(p_dress * 0.85, abs=1e-5)
    assert p[g]["draw"] == 1.0 and p[g]["mask"] == 0.5 and p[g]["p_play"] == pytest.approx(0.8 * 0.5, abs=1e-5)
    old = sc.participation_record(params, {DTD: 0.85}, None)  # without in_sim: exactly the format-1 record
    assert old["format"] == 1 and "draw" not in old["persons"][DTD] and old["persons"][DTD]["mask"] == 0.85
    keys = [DTD]
    got = gf._play_grade(keys, np.zeros((10, 1), np.int32), None, None, rec, {DTD: "F"}, {DTD: True})
    assert got["status"].startswith("saved at build (p_play = dress or start probability x in-simulation participation draw")
    assert got["F"]["brier"] == pytest.approx(round((p_dress * 0.85 - 1.0) ** 2, 4))
    got1 = gf._play_grade(keys, np.zeros((10, 1), np.int32), None, None, old, {DTD: "F"}, {DTD: True})
    assert got1["status"] == "saved at build (p_play = dress or start probability x participation mask)"


def test_in_draws_reads_the_record_and_the_resimulated_teams():
    from nhl_dfs.build import scenario_cache as sc

    new = {"participation": {"format": 2, "persons": {DTD: {"draw": 0.85}, "CCC-F00|CCC|F": {"draw": 0.5},
                                                       "AAA-G0|AAA|G": {"draw": 1.0}}}}
    old = {"participation": {"format": 1, "persons": {DTD: {"mask": 0.85}}}}
    assert sc.in_draws(old) == {} and sc.in_draws({}) == {}
    assert sc.in_draws(new) == {DTD: 0.85, "CCC-F00|CCC|F": 0.5}
    # AAA re-simulated: its cached record no longer applies; what it was re-simulated with does
    assert sc.in_draws(new, frozenset({"AAA", "BBB"}), {"BBB-F03|BBB|F": 0.5, "DDD-F00|DDD|F": 0.5}) == {
        "CCC-F00|CCC|F": 0.5, "BBB-F03|BBB|F": 0.5}


def test_a_game_hash_moves_only_for_the_game_with_a_dtd_skater():
    from nhl_dfs.build import scenario_cache as sc

    params = synthetic_params(TEAMS)
    a, b = slate_for(params, GAMES), slate_for(params, GAMES)
    ha = sc.game_hashes(a, params)
    b.participation = {DTD: 0.85}
    hb = sc.game_hashes(b, params)
    assert ha["DDD@CCC"] == hb["DDD@CCC"] and ha["BBB@AAA"] != hb["BBB@AAA"]
    b.participation = {DTD: 0.5}
    assert sc.game_hashes(b, params)["BBB@AAA"] != hb["BBB@AAA"]


# -- exactly once in each path (flag 101), on the mini Classic fixture (Jake DeBrusk, VAN, DK DTD) ------------------------------

DEB = "jake debrusk|VAN|F"
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


class _Spy:
    """Wraps a function or class attribute and keeps (args, kwargs, result) of every call."""

    def __init__(self, owner, name):
        self.owner, self.name, self.real, self.calls = owner, name, getattr(owner, name), []

    def __call__(self, *a, **k):
        out = self.real(*a, **k)
        self.calls.append((a, k, out))
        return out

    def __enter__(self):
        setattr(self.owner, self.name, self)
        return self

    def __exit__(self, *exc):
        setattr(self.owner, self.name, self.real)


def _deb_cols(scen, inputs):
    from nhl_dfs.intake.salary import read_salary

    pool = read_salary(inputs / "DKSalaries.csv")
    return [scen.col[x.role_id] for x in pool.rows if x.person_key == DEB and x.role_id in scen.col]


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    from datetime import datetime, timezone

    from conftest import mini_pair
    from nhl_dfs.build import objectives as ob
    from nhl_dfs.build.run import run_slate

    tmp = tmp_path_factory.mktemp("c24_mini")
    with _Spy(ob, "scenario_set") as spy:
        r = run_slate(*mini_pair("classic"), offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                      out_root=tmp / "runs", outputs_root=tmp / "outputs",
                      clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert r.ok, r.manifest["failed"]
    return tmp, r, spy.calls


def test_the_run_draws_the_dtd_skater_inside_the_game_and_never_masks_him(mini):
    from nhl_dfs.build import scenario_cache as sc
    from nhl_dfs.sim.score import FLAG_NAMES

    tmp, r, calls = mini
    part = r.manifest["scenario"]["participation"]
    assert part["in_simulation"] == [DEB] and part["masked"] == [] and part["persons"] == [DEB]
    got = sc.load(r.run.path, r.run.run_id)
    rec = got.meta["participation"]["persons"][DEB]
    assert got.meta["participation"]["format"] == 2 and rec["draw"] == 0.85 and rec["mask"] == 1.0
    assert got.meta["play_prob"] == {DEB: 0.85}  # the full dictionary, as before C24 (the controller reuses it)
    j = got.person_keys.index(DEB)
    d = np.concatenate([got.flags(p, got.n(p))[:, j, FLAG_NAMES.index("dressed")] for p in sc.PURPOSES])
    p, pp = rec["p_sim"] * 0.85, rec["p_sim"] * 0.85 ** 2
    se = np.sqrt(p * (1 - p) / d.size)
    assert abs(d.mean() - p) <= 4 * se and abs(d.mean() - pp) > 4 * se  # sat in 15% of his dressed games, once
    by = {k["purpose"]: out for a, k, out in calls}
    for purpose in sc.PURPOSES:  # the run's scores for him ARE his cached draws: nothing applied on top
        s = by[purpose]
        cached = got.base(purpose, got.n(purpose))[:, j]
        cols = _deb_cols(s, r.run.inputs)
        assert cols and all(np.array_equal(s.base[: cached.size, c], cached) for c in cols)


def _objective(r, meta=None):
    """The C10 controller's objective: rm=None, the cache as saved (or with its meta replaced)."""
    from datetime import datetime, timezone

    from nhl_dfs.build import packet
    from nhl_dfs.build import scenario_cache as sc
    from nhl_dfs.build import swap_objective

    got = sc.load(r.run.path, r.run.run_id)
    if meta is not None:
        got.meta = meta
    v = packet.RunView(r.run)
    so = swap_objective.ScenarioObjective(got, pool=v.pool, work=v.pool, rm=None, st=v.st, started_games=set(), n_use=800,
                                          now=datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc), odds_snapshot=None, notes=[])
    return got, so


def test_the_controller_and_the_dk_fallback_never_mask_a_skater_the_draws_carry(mini):
    import copy

    tmp, r, _ = mini
    goalie = next(k for k in _keys(r) if k.endswith("|G"))
    for meta_play in ({DEB: 0.85, goalie: 0.5}, None):  # the cache's own play_prob (with a goalie added), then the DK fallback
        got, _ = _objective(r)
        meta = copy.deepcopy(got.meta)
        meta["play_prob"] = meta_play
        got, so = _objective(r, meta)
        j = got.person_keys.index(DEB)
        cached = got.base("selection", 800)[:, j]
        s = so.sets["selection"]
        cols = _deb_cols(s, r.run.inputs)
        assert cols and all(np.array_equal(s.base[:, c], cached) for c in cols)
        assert so.record["participation_in_draws"] == 1
        if meta_play is not None:  # the goalie is masked, exactly as before C24
            jg = got.person_keys.index(goalie)
            gcols = [s.col[x] for x, pk in _role_persons(r).items() if pk == goalie and x in s.col]
            assert so.record["participation_masked"] == 1 and gcols
            assert not np.array_equal(s.base[:, gcols[0]], got.base("selection", 800)[:, jg])


def test_a_cache_made_before_c24_is_masked_exactly_as_before(mini):
    import copy

    from nhl_dfs.build import objectives as ob

    tmp, r, _ = mini
    got, _ = _objective(r)
    meta = copy.deepcopy(got.meta)
    for rec in meta["participation"]["persons"].values():  # back to a format-1 record
        rec.pop("draw", None)
    meta["participation"]["format"] = 1
    got, so = _objective(r, meta)
    j = got.person_keys.index(DEB)
    s = so.sets["selection"]
    want = np.where(ob.play_mask(800, so.keys, {DEB: 0.85}, got.seed, int(meta["purposes"]["selection"]["code"]))[:, so.keys.index(DEB)],
                    got.base("selection", 800)[:, j], 0)
    cols = _deb_cols(s, r.run.inputs)
    assert cols and all(np.array_equal(s.base[:, c], want) for c in cols)
    assert not np.array_equal(want, got.base("selection", 800)[:, j])  # the mask did bite
    assert so.record["participation_in_draws"] == 0 and so.record["participation_masked"] == 1


def _keys(r):
    from nhl_dfs.build import scenario_cache as sc

    return sc.load(r.run.path, r.run.run_id).person_keys


def _role_persons(r):
    from nhl_dfs.intake.salary import read_salary

    return {x.role_id: x.person_key for x in read_salary(r.run.inputs / "DKSalaries.csv").rows}


def _fresh_out(r, tmp_path):
    from nhl_dfs.intake.entries import read_entries
    from test_late_swap_objective import _fresh_with_out, _v3, _victim

    eid = read_entries(_v3(r)).entries[0].entry_id
    victim = _victim(r, eid)
    return _fresh_with_out(r.run.inputs / "DKSalaries.csv", tmp_path / "fresh.csv", [victim]), victim


def test_late_swap_resimulates_with_the_draw_and_never_masks_him(mini, tmp_path):
    from datetime import datetime, timezone

    from nhl_dfs.build import late_swap
    from nhl_dfs.build import objectives as ob
    from test_late_swap_objective import _v3

    tmp, r, _ = mini
    fresh, _ = _fresh_out(r, tmp_path)
    with _Spy(ob, "play_mask") as masks, _Spy(ob, "split_play") as splits:
        s = late_swap.run(r.run.run_id, _v3(r), offline=True, runs_root=tmp / "runs", outputs_root=tmp_path / "outputs",
                          salary_path=fresh, as_of=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    assert s.ok, s.manifest["failed"]
    o = s.manifest["objective"]["scenario"]
    assert o["games_resimulated"] == ["VAN@EDM"] and o["participation_in_draws"] == 1 and o["participation_masked"] == 0
    assert any(out[0] == {DEB: 0.85} for _, _, out in splits.calls)  # VAN@EDM re-simulated with him drawn inside the game
    assert all(DEB in k.get("in_sim", ()) for a, k, _ in masks.calls if DEB in a[2])  # never masked on top


def test_refresh_carries_the_draw_into_its_child_cache_and_never_masks_him(mini, tmp_path):
    from datetime import datetime, timezone

    from nhl_dfs.build import refresh
    from nhl_dfs.build import scenario_cache as sc
    from nhl_dfs.build import swap_objective
    from nhl_dfs.sim.score import FLAG_NAMES

    tmp, r, _ = mini
    fresh, _ = _fresh_out(r, tmp_path)
    made = []
    real_init = swap_objective.ScenarioObjective.__init__

    def keep(self, *a, **k):
        real_init(self, *a, **k)
        made.append(self)

    swap_objective.ScenarioObjective.__init__ = keep
    try:
        f = refresh.run(r.run.run_id, offline=True, runs_root=tmp / "runs", outputs_root=tmp_path / "outputs", salary_path=fresh,
                        as_of=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    finally:
        swap_objective.ScenarioObjective.__init__ = real_init
    assert f.ok, f.manifest["failed"]
    child = sc.load(f.run.path, f.run.run_id)
    rec = child.meta["participation"]
    assert rec["format"] == 2 and rec["persons"][DEB]["draw"] == 0.85 and rec["persons"][DEB]["mask"] == 1.0
    assert sc.in_draws(child.meta) == {DEB: 0.85}
    j = child.person_keys.index(DEB)
    n = child.n("selection")
    d = child.flags("selection", n)[:, j, FLAG_NAMES.index("dressed")]
    p = rec["persons"][DEB]["p_sim"] * 0.85
    assert abs(d.mean() - p) <= 4 * np.sqrt(p * (1 - p) / n)
    s = made[-1].sets["selection"]
    cols = _deb_cols(s, r.run.inputs)
    assert cols and all(np.array_equal(s.base[:, c], child.base("selection", n)[:, j]) for c in cols)


def test_nothing_changed_means_no_dtd_game_is_resimulated(mini, tmp_path):
    """The late-swap split equals the run's, so a game whose DTD skaters and probabilities did not change hashes as cached."""
    from datetime import datetime, timezone

    from nhl_dfs.build import refresh

    tmp, r, _ = mini
    f = refresh.run(r.run.run_id, offline=True, runs_root=tmp / "runs", outputs_root=tmp_path / "outputs",
                    as_of=datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert f.ok, f.manifest["failed"]
    o = f.manifest["objective"]["scenario"]
    assert o["games_resimulated"] == [] and o["participation_in_draws"] == 1 and o["participation_masked"] == 0
