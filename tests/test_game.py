"""C6: the aggregate joint simulator. Conservation, structure, reproducibility, dispersion."""

import copy

import numpy as np
import pytest

from nhl_dfs.models import params as params_mod
from nhl_dfs.sim import game, market, score
from nhl_dfs.sim import outcomes as oc
from sim_helpers import MODEL_CFG, slate_for, synthetic_params

pytestmark = pytest.mark.c6

N = 3000


@pytest.fixture(scope="module")
def full():
    """Complete rosters (every listed skater dresses: no phantom weight), two games."""
    params = synthetic_params(("AAA", "BBB", "CCC", "DDD"), n_f=12, n_d=6, p_dress=1.0)
    slate = slate_for(params, games=(("AAA", "BBB"), ("CCC", "DDD")))
    return params, slate, game.simulate(slate, params, N, seed=11)


def _team_cols(o, team, goalies=False):
    return [i for i, k in enumerate(o.person_keys) if k.split("|")[1] == team and k.endswith("|G") == goalies]


def test_goals_conserve_and_assists_never_exceed_two_per_goal(full):
    _, _, o = full
    for t, team in enumerate(o.teams):
        cols = _team_cols(o, team)
        assert np.array_equal(o.goals[:, cols].sum(axis=1), o.team_goals[:, t])  # no goal without a scorer
        assert np.all(o.assists[:, cols].sum(axis=1) <= 2 * o.goals[:, cols].sum(axis=1))
    assert 1.3 < o.assists.sum() / o.goals.sum() < 2.0  # config: 1.686 assists per goal


def test_an_assister_is_never_the_scorer_and_shares_the_ice(full):
    params, slate, _ = full
    game.EVENT_LOG = []
    try:
        game.simulate(slate, params, 800, seed=3)
        log = list(game.EVENT_LOG)
    finally:
        game.EVENT_LOG = None
    assert log
    same_unit = total = 0
    for team, state, rows, sc_i, a1, a2, dressed in log:
        names = sorted(k.split("|")[0] for k in params.persons if k.split("|")[1] == team and not k.endswith("|G"))
        line_of = np.array([int(nm.split("-F")[1]) // 3 if "-F" in nm else -1 for nm in names] + [-2, -3])
        assert not np.any(a1 == sc_i) and not np.any(a2 == sc_i)
        both = (a1 >= 0) & (a2 >= 0)
        assert not np.any(a1[both] == a2[both])
        m = np.arange(rows.size)
        assert dressed[m, sc_i].all()  # only dressed people score
        has1 = a1 >= 0
        assert dressed[m[has1], a1[has1]].all()
        has2 = a2 >= 0
        assert dressed[m[has2], a2[has2]].all()
        if state == game.EV:  # forwards A..: same line, or the defense pair, as the scorer
            ok = has1 & (line_of[sc_i] >= 0) & (line_of[np.maximum(a1, 0)] >= 0)
            same_unit += int(np.sum(line_of[sc_i[ok]] == line_of[a1[ok]]))
            total += int(ok.sum())
    assert total > 100 and same_unit / total > 0.6  # forward assisters come mostly from the scorer's line


def test_saves_plus_goals_against_equal_opposing_shots_and_empty_net_goals_are_not_charged(full):
    _, _, o = full
    tix = {t: i for i, t in enumerate(o.teams)}
    for own, opp in (("AAA", "BBB"), ("BBB", "AAA"), ("CCC", "DDD"), ("DDD", "CCC")):
        cols = _team_cols(o, own, goalies=True)
        saves = o.saves[:, cols].astype(int).sum(axis=1)
        ga = o.ga[:, cols].astype(int).sum(axis=1)
        opp_sog = o.team_sog[:, tix[opp]].astype(int)
        opp_en = o.team_en[:, tix[opp]].astype(int)
        assert np.array_equal(saves + ga, opp_sog - opp_en)  # shots faced exclude the empty net
        assert np.array_equal(ga, o.team_goals[:, tix[opp]].astype(int) - opp_en)
    assert o.team_en.sum() > 0


def test_every_game_has_one_winning_and_one_losing_goalie_of_record_and_shutouts_are_clean(full):
    _, _, o = full
    for (a, b) in (("AAA", "BBB"), ("CCC", "DDD")):
        cols = _team_cols(o, a, True) + _team_cols(o, b, True)
        dec = o.decision[:, cols]
        assert np.all((dec == oc.DECISIONS["W"]).sum(axis=1) == 1)
        assert np.all(((dec == oc.DECISIONS["L"]) | (dec == oc.DECISIONS["OTL"])).sum(axis=1) == 1)
    gcols = [i for i in range(o.p) if o.is_goalie[i]]
    assert np.all(o.ga[:, gcols][o.shutout[:, gcols] > 0] == 0)
    assert o.shutout.sum() > 0
    # OTL exists only in games that went past regulation
    gi = {g: i for i, g in enumerate(o.games)}
    for (a, b, key) in (("AAA", "BBB", "BBB@AAA"), ("CCC", "DDD", "DDD@CCC")):
        cols = _team_cols(o, a, True) + _team_cols(o, b, True)
        otl = (o.decision[:, cols] == oc.DECISIONS["OTL"]).any(axis=1)
        assert np.array_equal(otl, o.game_tie[:, gi[key]])


def test_shootout_goals_have_their_own_column(full):
    _, _, o = full
    gi = {g: i for i, g in enumerate(o.games)}
    so_rows = o.game_so[:, gi["BBB@AAA"]]
    cols = _team_cols(o, "AAA") + _team_cols(o, "BBB")
    per_row = o.so_goals[:, cols].sum(axis=1)
    assert np.all(per_row[~so_rows] == 0) and np.all(per_row[so_rows] >= 1)
    t = {k: i for i, k in enumerate(o.teams)}
    tot_goals = o.goals[:, cols].sum(axis=1)
    assert np.array_equal(tot_goals, o.team_goals[:, [t["AAA"], t["BBB"]]].sum(axis=1))  # SO goals are not goals


def test_an_undressed_person_has_no_minutes_and_no_events():
    params = synthetic_params(("AAA", "BBB"), n_f=14, n_d=8, p_dress=0.5)
    o = game.simulate(slate_for(params), params, 1500, seed=2)
    sk = ~o.is_goalie
    off = ~o.dressed & sk[None, :]
    assert off.sum() > 1000
    for name in ("goals", "assists", "sog", "blocks", "sh_pts", "so_goals"):
        assert not getattr(o, name)[off].any(), name
    assert not score.base_tenths(o)[off].any()
    assert 0.4 < o.dressed[:, sk].mean() < 0.6


def test_same_seed_reproduces_bit_for_bit_and_chunks_are_seed_stable(full):
    params, slate, o = full
    again = game.simulate(slate, params, N, seed=11)
    other = game.simulate(slate, params, N, seed=12)
    assert np.array_equal(score.base_tenths(o), score.base_tenths(again))
    assert not np.array_equal(score.base_tenths(o), score.base_tenths(other))
    assert score.base_tenths(o).dtype == np.int32
    # the first chunk of a larger run is identical to a run of exactly that chunk
    size = slate.cfg["chunk_size"]
    small = game.simulate(slate, params, size, seed=11)
    big = game.simulate(slate, params, size + 500, seed=11)
    assert np.array_equal(score.base_tenths(small), score.base_tenths(big)[:size])
    # purposes draw from separate streams
    ref = game.simulate(slate, params, 500, seed=11, purpose="referee")
    assert not np.array_equal(score.base_tenths(ref), score.base_tenths(small)[:500])


def test_person_axis_is_sorted(full):
    _, _, o = full
    assert o.person_keys == sorted(o.person_keys)


def test_pace_makes_team_shots_more_dispersed_than_independent_draws():
    params = synthetic_params(("AAA", "BBB"), n_f=13, n_d=7)
    base = slate_for(params)
    flat_cfg = copy.deepcopy(base.cfg)
    flat_cfg["pace"]["var"] = 1e-6
    flat = slate_for(params)
    flat.cfg = flat_cfg
    n = 6000
    with_pace = game.simulate(base, params, n, seed=5).team_sog.astype(float)
    without = game.simulate(flat, params, n, seed=5).team_sog.astype(float)
    assert with_pace.var(axis=0).mean() > without.var(axis=0).mean() * 1.05
    # target: residual team-SOG spread measured from 2,624 games (sd 6.19 on a mean of 28.05)
    cv = float(np.mean(with_pace.std(axis=0) / with_pace.mean(axis=0)))
    assert cv == pytest.approx(6.186 / 28.05, rel=0.10)


def test_person_shot_tail_matches_the_c5_negative_binomial():
    """Holding each person's marginal at C5's mu + phi mu^2 (the pace and TOI mixing must not stack
    extra dispersion on top): P(SOG >= 5 | dressed) and P(blocks >= 3 | dressed) match the analytic tails."""
    params = synthetic_params(("AAA", "BBB"), n_f=12, n_d=6, p_dress=1.0)
    o = game.simulate(slate_for(params), params, 20000, seed=9)
    checked = 0
    for k, p in params.persons.items():
        if p.group == "G":
            continue
        c = o.person_keys.index(k)
        r, op = p.rates, p.opportunity
        mu_sog = float(o.sog[:, c].mean())
        mu_blk = float(o.blocks[:, c].mean())
        for stat, mu, phi, thr in (("sog", mu_sog, r.phi_sog, 5), ("blocks", mu_blk, r.phi_blk, 3)):
            got = float((getattr(o, stat)[:, c] >= thr).mean())
            want = params_mod._nb_tail(thr, mu, phi)
            se = np.sqrt(max(want * (1 - want), 1e-4) / 20000)
            assert abs(got - want) < 4 * se + 0.012, (k, stat, got, want)
            checked += 1
    assert checked >= 40


def test_pool_missing_skaters_get_phantoms_not_extra_goals():
    """Six listed skaters cannot absorb a team's goals: the rest go to unlisted (phantom) players."""
    params = synthetic_params(("AAA", "BBB"), n_f=3, n_d=1, p_dress=1.0)
    o = game.simulate(slate_for(params), params, 2000, seed=4)
    t = 0
    cols = _team_cols(o, "AAA")
    assert o.goals[:, cols].sum(axis=1).mean() < 0.55 * o.team_goals[:, t].mean()
    # ... and goalie saves still see every shot, listed or not
    g = _team_cols(o, "BBB", True)
    assert (o.saves[:, g].sum(axis=1) + o.ga[:, g].sum(axis=1)).mean() > 20


def test_memory_cap_refuses_rather_than_resizing():
    params = synthetic_params(("AAA", "BBB"))
    slate = slate_for(params)
    slate.cfg = copy.deepcopy(slate.cfg)
    slate.cfg["memory_cap_mb"] = 0.001
    with pytest.raises(MemoryError):
        game.simulate(slate, params, 100, seed=1)


def test_model_strength_reads_the_param_table():
    params = synthetic_params(("AAA", "BBB"), n_f=13, n_d=7)
    s = market.model_strength(params, "AAA", "BBB", model_cfg=MODEL_CFG)
    assert 2.0 < s.lam_home < 3.6 and 2.0 < s.lam_away < 3.6
    thin = synthetic_params(("AAA", "BBB"), n_f=3, n_d=1)
    assert market.model_strength(thin, "AAA", "BBB", model_cfg=MODEL_CFG).notes  # league rate, and it says so


def test_cache_writes_chunks_with_hashes_and_matches_a_direct_run(tmp_path):
    from nhl_dfs.sim import cache

    params = synthetic_params(("AAA", "BBB"), n_f=13, n_d=7)
    slate = slate_for(params)
    n = slate.cfg["chunk_size"] + 300  # two chunks, the second short
    info = cache.build(tmp_path / "sim", slate, params, n, seed=21, purpose="selection")
    meta = cache.read_meta(tmp_path / "sim")
    assert meta["chunks"] == 2 and meta["n"] == n and meta["seed"] == 21 and meta["purpose"] == "selection"
    assert meta["dtype"] == "int32" and meta["person_keys"] == sorted(params.persons)
    assert info.spec_sha256 == cache.spec_hash(slate, params, 21, "selection", n)
    assert cache.is_current(tmp_path / "sim", info.spec_sha256)
    assert not cache.is_current(tmp_path / "sim", cache.spec_hash(slate, params, 22, "selection", n))
    direct = score.base_tenths(game.simulate(slate, params, n, seed=21, purpose="selection"))
    assert np.array_equal(cache.load_base(tmp_path / "sim"), direct)  # same draws, chunked or not
    assert info.bytes < 3_000_000 and info.summary["team"]["goals"][0] > 2.0
    # rebuilding replaces the old chunks
    cache.build(tmp_path / "sim", slate, params, 500, seed=21)
    assert len(list((tmp_path / "sim").glob("chunk_*.npy"))) == 1


def test_showdown_pool_roles_end_to_end_and_the_captain_counts_once():
    """A real Showdown pool structure (CPT and FLEX rows per person): the role columns of one person are
    identical in every scenario, and a lineup's captain multiplier changes its score exactly once."""
    from conftest import MINI
    from nhl_dfs.intake.salary import read_salary
    from nhl_dfs.sim.slate import build_slate

    pool = read_salary(MINI / "showdown" / "DKSalaries.csv")
    table = params_mod.projection_for(pool, __import__("datetime").date(2026, 9, 28))
    slate, _ = build_slate(pool, table, None)
    o = game.simulate(slate, table, 600, seed=17)
    role_ids, cols = score.role_map_for(pool, o.person_keys)
    rb = score.role_tenths(score.base_tenths(o), cols)
    by_person = {}
    for i, rid in enumerate(role_ids):
        by_person.setdefault(pool.by_role_id[rid].person_key, []).append(i)
    pairs = [v for v in by_person.values() if len(v) == 2]
    assert pairs
    for a, b in pairs:
        assert np.array_equal(rb[:, a], rb[:, b])  # CPT and FLEX rows are the same realized stat line
    cpt = next(i for i, rid in enumerate(role_ids) if "CPT" in pool.by_role_id[rid].roster_positions)
    flex = [i for i, rid in enumerate(role_ids) if "FLEX" in pool.by_role_id[rid].roster_positions and i not in by_person[
        pool.by_role_id[role_ids[cpt]].person_key]][:5]
    lineup = np.array([[cpt] + flex])
    plain = score.lineup_twentieths(rb, lineup, captain_col=None)
    capt = score.lineup_twentieths(rb, lineup, captain_col=0)
    assert np.array_equal(capt - plain, rb[:, [cpt]])  # exactly one extra 1x of the captain's base
