import random

import pytest
from conftest import mini_pair

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, SHOWDOWN_SLOTS, Mode, check_lineup, slot_accepts
from nhl_dfs.intake.entries import existing_lineup, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee import rules
from nhl_dfs.referee.reader import read_salary_min

pytestmark = pytest.mark.c0b

N_DRAWS = 1000
SEED = 20260929


def _draw(rng, pool, slots):
    """Mostly slot-eligible picks with occasional off-slot rows and repeats, so
    both rule sets see legal and illegal lineups (slot, cap, team, duplicate)."""
    picks = []
    for slot in slots:
        eligible = [r for r in pool.rows if slot_accepts(slot, r, pool.mode)]
        fresh = [r for r in eligible if r.person_key not in {p.person_key for p in picks}]
        if fresh and rng.random() < 0.9:
            eligible = fresh
        if rng.random() < 0.04:
            picks.append(rng.choice(pool.rows))
        elif picks and rng.random() < 0.02:
            picks.append(rng.choice(picks))
        else:
            picks.append(rng.choice(eligible))
    return picks


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_referee_rules_agree_with_geometry_on_random_lineups(mode):
    sal = mini_pair(mode)[0]
    pool = read_salary(sal)
    ref = read_salary_min(sal)
    slots = CLASSIC_SLOTS if pool.mode is Mode.CLASSIC else SHOWDOWN_SLOTS
    rng = random.Random(SEED)
    legal_count = 0
    for i in range(N_DRAWS):
        picks = _draw(rng, pool, slots)
        geo = check_lineup(picks, pool.mode)
        ok, why = rules.legal([ref.rows[r.role_id] for r in picks], mode)
        assert geo.ok == ok, (i, [r.role_id for r in picks], geo.reasons, why)
        legal_count += ok
    share = legal_count / N_DRAWS
    assert 0.2 <= share <= 0.8, f"{mode}: legal share {share:.2f} makes agreement vacuous"


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_same_name_different_people_are_legal_together_under_both(mode):
    sal, ent = mini_pair(mode)
    pool, entries, ref = read_salary(sal), read_entries(ent), read_salary_min(sal)
    checked = 0
    for entry in entries.entries:
        lineup = existing_lineup(entry, entries)
        if not all(lineup):
            continue
        rows = [pool.by_role_id[rid] for rid in lineup]
        names = [r.name for r in rows]
        if names.count("Elias Pettersson") < 2:
            continue
        assert check_lineup(rows, pool.mode).ok
        assert rules.legal([ref.rows[r.role_id] for r in rows], mode)[0]
        checked += 1
    assert checked >= 1
