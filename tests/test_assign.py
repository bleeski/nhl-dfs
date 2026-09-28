from datetime import datetime, timedelta, timezone

import pytest

from conftest import mini_pair
from nhl_dfs.build.assign import Caps, arrange_util, assign, load_caps
from nhl_dfs.build.candidates import Candidate, generate
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, Mode, check_lineup, lineup_key
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models.priors import prior_objective, prior_table
from pool_builder import classic_pool, showdown_pool

pytestmark = pytest.mark.c2b

T0 = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)


def _cand(pool, ids, value):
    rows = [pool.by_role_id[r] for r in ids]
    assert check_lineup(rows, pool.mode).ok, check_lineup(rows, pool.mode).reasons
    return Candidate(tuple(ids), lineup_key(rows, pool.mode), float(value), "base")


def _classic_ids(pool, picks):
    """picks: list of (team, position) in canonical slot order; takes the next unused row."""
    used, out = set(), []
    for team, pos in picks:
        r = next(r for r in pool.rows if r.team == team and r.position == pos and r.role_id not in used)
        used.add(r.role_id)
        out.append(r.role_id)
    return out


BASE = [("AAA", "C"), ("BBB", "C"), ("AAA", "LW"), ("BBB", "LW"), ("CCC", "LW"),
        ("AAA", "D"), ("BBB", "D"), ("CCC", "C"), ("AAA", "G")]


@pytest.fixture
def pool():
    return classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))


def _variant(pool, swap_index, team, pos):
    picks = list(BASE)
    picks[swap_index] = (team, pos)
    return _classic_ids(pool, picks)


def test_load_caps_matches_config():
    caps = load_caps()
    assert caps == Caps(0.6, 0.4, 7, 0.25)
    assert caps.person_cap(1) == 1 and caps.person_cap(5) == 3 and caps.captain_cap(2) == 1


def test_best_objective_goes_first_and_candidates_are_distinct():
    pool = read_salary(mini_pair("classic")[0])
    obj = prior_objective(pool, prior_table(pool))
    cands = generate(pool, pool.mode, obj, 12, seed=1, perturb_sd=2.0)
    a = assign(cands, ["e1", "e2", "e3"], pool, pool.mode, Caps(1.0, 1.0, 9, 0.0), seed=0)
    best = max(cands, key=lambda c: c.objective_value)
    assert set(a.by_entry["e1"]) == set(best.role_ids)
    assert len({lineup_key([pool.by_role_id[r] for r in v], pool.mode) for v in a.by_entry.values()}) == 3
    assert not a.relaxations


def test_classic_overlap_cap_skips_a_near_copy(pool):
    top = _cand(pool, _classic_ids(pool, BASE), 100)
    near = _cand(pool, _variant(pool, 4, "DDD", "LW"), 99)  # shares 8 people with top
    far = _cand(pool, _classic_ids(pool, [("DDD", "C"), ("CCC", "C"), ("DDD", "LW"), ("CCC", "RW"), ("BBB", "RW"),
                                          ("DDD", "D"), ("CCC", "D"), ("BBB", "C"), ("DDD", "G")]), 50)
    a = assign([top, near, far], ["e1", "e2"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 7, 0.0), seed=0)
    assert a.by_entry["e2"] == far.role_ids
    assert a.overlap_max <= 7 and not a.relaxations


def test_overlap_is_relaxed_first_and_recorded(pool):
    top = _cand(pool, _classic_ids(pool, BASE), 100)
    near = _cand(pool, _variant(pool, 4, "DDD", "LW"), 99)
    a = assign([top, near], ["e1", "e2"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 7, 0.0), seed=0)
    assert a.by_entry["e2"] == near.role_ids
    assert [(r.entry_id, r.kind) for r in a.relaxations] == [("e2", "OVERLAP")]
    assert a.overlap_max == 8


def test_person_cap_then_exposure_relaxation(pool):
    top = _cand(pool, _classic_ids(pool, BASE), 100)
    other = _cand(pool, _variant(pool, 4, "DDD", "LW"), 99)
    # caps: 2 entries * 0.5 -> 1 per person; every candidate shares people with top
    a = assign([top, other], ["e1", "e2"], pool, Mode.CLASSIC, Caps(0.5, 1.0, 9, 0.0), seed=0)
    assert [(r.entry_id, r.kind) for r in a.relaxations] == [("e2", "EXPOSURE")]
    assert max(a.person_exposures.values()) == 2


def test_repeat_is_the_last_resort_and_recorded(pool):
    only = _cand(pool, _classic_ids(pool, BASE), 100)
    a = assign([only], ["e1", "e2", "e3"], pool, Mode.CLASSIC, Caps(), seed=0)
    assert all(v == only.role_ids for v in a.by_entry.values())
    assert [r.kind for r in a.relaxations] == ["REPEAT", "REPEAT"]
    assert a.exposures == {only.key: 3}


def test_showdown_captain_cap():
    pool = showdown_pool(per_team=6)
    p = list(pool.persons.values())  # 0-5 AAA, 6-11 BBB
    star = p[0]

    def lineup(cpt, flex):
        return [p[cpt].cpt.role_id] + [p[i].flex.role_id for i in flex]

    c1 = _cand(pool, lineup(0, [1, 2, 6, 7, 8]), 100)
    c2 = _cand(pool, lineup(0, [3, 4, 9, 10, 11]), 99)
    c3 = _cand(pool, lineup(1, [0, 5, 6, 9, 10]), 90)
    a = assign([c1, c2, c3], ["e1", "e2"], pool, Mode.SHOWDOWN, Caps(1.0, 0.5, 9, 0.0), seed=0)
    assert a.by_entry["e2"] == c3.role_ids
    assert a.captain_exposures[star.cpt.person_key] == 1


def test_util_gets_the_later_skater_without_changing_players(pool):
    ids = _classic_ids(pool, BASE)
    starts = {r.role_id: T0 for r in pool.rows}
    starts[ids[0]] = T0 + timedelta(hours=3)  # the first C starts latest
    out = arrange_util(ids, pool, starts)
    assert out[CLASSIC_SLOTS.index("UTIL")] == ids[0] and out[0] == ids[7]
    assert set(out) == set(ids)
    assert check_lineup([pool.by_role_id[r] for r in out], Mode.CLASSIC).ok


def test_util_swap_needs_both_slots_to_accept(pool):
    ids = _classic_ids(pool, BASE)  # UTIL holds a C
    starts = {r.role_id: T0 for r in pool.rows}
    starts[ids[5]] = T0 + timedelta(hours=3)  # a D starts latest; the C cannot play D
    assert arrange_util(ids, pool, starts) == tuple(ids)


def test_tie_band_prefers_a_later_util(pool):
    early = _cand(pool, _classic_ids(pool, BASE), 100.0)
    late_ids = _variant(pool, 7, "DDD", "C")
    late = _cand(pool, late_ids, 99.9)
    starts = {r.role_id: T0 for r in pool.rows}
    starts[late_ids[7]] = T0 + timedelta(hours=3)
    inside = assign([early, late], ["e1"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 9, 0.25), seed=0, later_start_utc=starts)
    outside = assign([early, late], ["e1"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 9, 0.05), seed=0, later_start_utc=starts)
    assert inside.by_entry["e1"] == late.role_ids
    assert outside.by_entry["e1"] == early.role_ids


def test_fixed_entries_are_kept_and_count_toward_caps(pool):
    top = _cand(pool, _classic_ids(pool, BASE), 100)
    near = _cand(pool, _variant(pool, 4, "DDD", "LW"), 99)
    far = _cand(pool, _classic_ids(pool, [("DDD", "C"), ("CCC", "C"), ("DDD", "LW"), ("CCC", "RW"), ("BBB", "RW"),
                                          ("DDD", "D"), ("CCC", "D"), ("BBB", "C"), ("DDD", "G")]), 50)
    a = assign([top, near, far], ["e1", "e2"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 7, 0.0), seed=0,
               fixed={"e1": near.role_ids})
    assert list(a.by_entry) == ["e1", "e2"]
    assert a.by_entry["e1"] == near.role_ids
    assert a.by_entry["e2"] == far.role_ids  # top shares 8 people with the fixed entry


def test_empty_bank_raises():
    with pytest.raises(ValueError, match="empty candidate bank"):
        assign([], ["e1"], classic_pool(), Mode.CLASSIC, Caps(), seed=0)


def test_seed_only_breaks_exact_ties(pool):
    a = _cand(pool, _classic_ids(pool, BASE), 100)
    b = _cand(pool, _variant(pool, 4, "DDD", "LW"), 100)
    picks = {assign([a, b], ["e1"], pool, Mode.CLASSIC, Caps(1.0, 1.0, 9, 0.0), seed=s).by_entry["e1"] for s in range(20)}
    assert picks == {a.role_ids, b.role_ids}
    again = [assign([a, b], ["e1"], pool, Mode.CLASSIC, Caps(), seed=3).by_entry["e1"] for _ in range(3)]
    assert len(set(again)) == 1
