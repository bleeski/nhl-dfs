import random
from datetime import datetime, timedelta, timezone

import pytest

from nhl_dfs.build import provisional
from nhl_dfs.build.assign import Caps
from nhl_dfs.build.candidates import generate
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.intake.entries import EntryRow
from nhl_dfs.models import field, ownership
from nhl_dfs.models.contests import load_contest_families, resolve
from nhl_dfs.models.priors import prior_objective, prior_table
from nhl_dfs.models.projection import PriorProjection
from pool_builder import varied_pool

pytestmark = pytest.mark.c3

LOOSE = Caps(person_max_share=1.0, captain_max_share=1.0, classic_max_overlap=9, util_tie_band_points=0.25)


@pytest.fixture(scope="module")
def fam_cfg():
    return load_contest_families()


@pytest.fixture(scope="module")
def own_cfg():
    return ownership.load_ownership_config()


def bank(mode, n=60, seed=0):
    pool = varied_pool(mode, seed=seed)
    proj = PriorProjection(pool)
    obj = prior_objective(pool, prior_table(pool))
    cands = generate(pool, mode, obj, n, seed=seed, perturb_sd=3.0, min_pairwise_diff=2)
    return pool, proj, cands


def fake_marginals(pool, seed=1, family="large_gpp"):
    rng = random.Random(seed)
    own = {r.role_id: rng.choice([0.0, 0.0, rng.uniform(0.5, 60.0)]) for r in pool.rows}
    return field.Marginals(family, 100, 1000, own, {}, {}, {}, {}, {}, 0, False)


def entries(spec):
    """[(contest name, contest id, count)] -> EntryRows."""
    out, n = [], 0
    for name, cid, k in spec:
        for _ in range(k):
            n += 1
            out.append(EntryRow(str(8_000_000 + n), name, cid, "$1", (), n, b""))
    return out


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("policy", ["own_then_dup", "dup_first", "mean"])
def test_never_prefers_a_candidate_more_than_one_band_worse(mode, policy, fam_cfg, own_cfg):
    pool, proj, cands = bank(mode)
    ranked = provisional.rank(cands, pool, proj, fake_marginals(pool), policy, fam_cfg, own_cfg=own_cfg)
    assert len(ranked) == len(cands)
    for i, a in enumerate(ranked):
        for b in ranked[i + 1:]:
            assert b.mean_tenths - a.mean_tenths <= a.band + 1e-9
    if policy == "mean":
        means = [s.mean_tenths for s in ranked]
        assert means == sorted(means, reverse=True)


def test_leverage_moves_inside_band_only(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    ranked = provisional.rank(cands, pool, proj, fake_marginals(pool), "own_then_dup", fam_cfg, own_cfg=own_cfg)
    first_band = [s for s in ranked if s.band_index == 0]
    assert first_band[0].own_pct == min(s.own_pct for s in first_band)
    assert ranked[0].mean_tenths >= max(s.mean_tenths for s in ranked) - ranked[0].band


def test_band_floor_can_exceed_the_percentage(fam_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    c = cands[0]
    mean = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {}, 1.0)
    from nhl_dfs.models.projection import lineup_sd_tenths

    floor = lineup_sd_tenths(pool, proj, c.role_ids) / 3.0  # sqrt(9 slots)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.0) == pytest.approx(floor)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.001) == pytest.approx(floor)
    assert provisional.band_width(pool, proj, c.role_ids, mean, 0.5) == pytest.approx(0.5 * mean)


def test_dtd_questionable_mean_is_haircut(fam_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    c = cands[0]
    q = fam_cfg["selection"]["questionable_play_prob"]
    rid = c.role_ids[0]
    base = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {}, q)
    hit = provisional.adjusted_mean_tenths(pool, proj, c.role_ids, {rid: Participation.QUESTIONABLE}, q)
    assert hit == pytest.approx(base - (1 - q) * proj.mean_tenths(rid))


def later_starts(pool, seed=3):
    rng = random.Random(seed)
    t0 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
    by_team = {t: t0 + timedelta(hours=rng.randint(0, 3)) for t in sorted(pool.teams)}
    return {r.role_id: by_team[r.team] for r in pool.rows}


@pytest.mark.parametrize("mode", list(Mode))
def test_each_pick_within_one_band_of_best_available(mode, fam_cfg, own_cfg):
    pool, proj, cands = bank(mode)
    ents = entries([("NHL $5K Sniper, 150 Max", "1", 6), ("NHL $3 Winner Take All", "2", 3),
                    ("NHL Single Entry $1 Double Up", "3", 3)])
    ctx = resolve(ents, cfg=fam_cfg)
    margs = {cid: fake_marginals(pool, seed=int(cid)) for cid in ctx}
    starts = later_starts(pool)
    a, scored = provisional.select(cands, proj, margs, ctx, ents, LOOSE, fam_cfg, seed=1, pool=pool,
                                   later_start_utc=starts, own_cfg=own_cfg)
    assert set(a.by_entry) == {e.entry_id for e in ents} and not a.relaxations
    used: set[str] = set()
    order = [e for f in fam_cfg["selection"]["family_order"] for e in ents if ctx[e.contest_id].family == f]
    for e in order:
        policy = fam_cfg["families"][ctx[e.contest_id].family]["selection"]
        ranked = provisional.rank(cands, pool, proj, margs[e.contest_id], policy, fam_cfg, own_cfg=own_cfg)
        avail = [s for s in ranked if s.cand.key not in used]
        pick = scored[e.entry_id]
        assert pick.cand.key == avail[0].cand.key  # no UTIL swap undid the order
        assert max(s.mean_tenths for s in avail) - pick.mean_tenths <= pick.band + 1e-9
        used.add(pick.cand.key)


def test_cash_entries_get_the_highest_mean_candidates(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC)
    ents = entries([("NHL $5K Sniper, 150 Max", "1", 4), ("NHL Single Entry $1 Double Up", "3", 3)])
    ctx = resolve(ents, cfg=fam_cfg)
    statuses = {pool.rows[0].role_id: Participation.QUESTIONABLE}
    margs = {cid: fake_marginals(pool) for cid in ctx}
    a, scored = provisional.select(cands, proj, margs, ctx, ents, LOOSE, fam_cfg, seed=1, pool=pool,
                                   statuses=statuses, own_cfg=own_cfg)
    q = fam_cfg["selection"]["questionable_play_prob"]
    means = sorted((provisional.adjusted_mean_tenths(pool, proj, c.role_ids, statuses, q) for c in cands), reverse=True)
    cash = sorted((scored[e.entry_id].mean_tenths for e in ents if e.contest_id == "3"), reverse=True)
    assert cash == pytest.approx(means[:3])


def test_caps_span_the_whole_portfolio(fam_cfg, own_cfg):
    pool, proj, cands = bank(Mode.CLASSIC, n=80)
    ents = entries([("NHL Single Entry $1 Double Up", "3", 5), ("NHL $5K Sniper, 150 Max", "1", 5)])
    ctx = resolve(ents, cfg=fam_cfg)
    caps = Caps(person_max_share=0.5, captain_max_share=0.4, classic_max_overlap=7)
    a, _ = provisional.select(cands, proj, {}, ctx, ents, caps, fam_cfg, seed=1, pool=pool, own_cfg=own_cfg)
    if not a.relaxations:
        assert max(a.person_exposures.values()) <= caps.person_cap(10)
