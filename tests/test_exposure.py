"""C8 exposure caps: feasibility floors, small portfolios, per-game cap only on multi-game slates."""

import numpy as np
import pytest

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.geometry import Mode
from pool_builder import classic_pool, showdown_pool

pytestmark = pytest.mark.c8

CFG = exposure.load_exposure_config()
BUDGET = ob.load_risk_config()["budget"]


def test_caps_with_two_goalies_raise_to_the_floor():
    pool = classic_pool()
    c = exposure.caps(CFG, 40, pool, Mode.CLASSIC, 2, usable_goalies=2, budget=BUDGET["classic"])
    assert c.goalie == 20  # 35% of 40 is 14, but two goalies must carry 40 entries
    assert c.floors["goalie"] == 20 and any("feasibility floor 20" in n for n in c.notes)
    assert c.person == 18  # floor(0.45 x 40)


def test_small_portfolios_are_not_forced_disjoint():
    pool = classic_pool()
    c = exposure.caps(CFG, 2, pool, Mode.CLASSIC, 4, tournament_entries=1, budget=BUDGET["classic"])
    assert c.person is None and c.goalie is None and c.max_overlap is None
    sd = showdown_pool()
    c2 = exposure.caps(CFG, 3, sd, Mode.SHOWDOWN, 1, usable_captains=6, budget=BUDGET["showdown"])
    assert c2.person is None and c2.captain == 1


def test_single_game_showdown_never_triggers_the_per_game_cap():
    sd = showdown_pool()
    c = exposure.caps(CFG, 40, sd, Mode.SHOWDOWN, 1, budget={**BUDGET["showdown"], "game_fee_share_max": 0.4})
    assert c.game_fee_share is None and any("single-game" in n for n in c.notes)
    classic = exposure.caps(CFG, 40, classic_pool(), Mode.CLASSIC, 3, budget=BUDGET["classic"])
    assert classic.game_fee_share == pytest.approx(0.40)


def test_fee_budget_below_the_achievable_floor_caps_lineups_instead():
    pool = classic_pool()
    c = exposure.caps(CFG, 2, pool, Mode.CLASSIC, 4, fees_cents=[100, 100], usable_goalies=6, budget=BUDGET["classic"])
    # two $1 entries: some goalie carries at least half the fees, so the 40% budget is a lineup cap (B36)
    assert c.goalie_fee_share is None and c.goalie_lineups == 1 and c.floors["goalie_fee_share_max"] == 0.5
    assert exposure.fee_floor([2000, 100, 100], 3) == pytest.approx(2000 / 2200)
    assert exposure.fee_floor([100] * 10, 5) == pytest.approx(0.2)


def test_unequal_fees_of_2026_10_01_switch_the_goalie_and_game_caps_to_lineups():
    """B36 acceptance: the $1 entry was 59% of $1.70, so no dollar cap at 40% exists; 40% of 5 entries is 2."""
    c = exposure.caps(CFG, 5, classic_pool(), Mode.CLASSIC, 4, fees_cents=[25, 25, 100, 10, 10], tournament_entries=5,
                      usable_goalies=8, budget=BUDGET["classic"])
    assert c.goalie_fee_share is None and c.goalie_lineups == 2
    assert c.game_fee_share is None and c.game_lineups == 2
    assert c.cap_status["GOALIE_CAP"] == "LINEUPS 2/5 (fee floor 0.59 > budget 0.40)"
    assert c.cap_status["GAME_CAP"].startswith("LINEUPS 2/5")
    assert any(n.startswith("GOALIE_CAP=LINEUPS 2/5") for n in c.notes)
    assert c.record()["goalie_lineups"] == 2


def test_equal_fees_keep_the_dollar_cap():
    c = exposure.caps(CFG, 5, classic_pool(), Mode.CLASSIC, 4, fees_cents=[100] * 5, usable_goalies=8,
                      budget=BUDGET["classic"])
    assert c.goalie_fee_share == pytest.approx(0.40) and c.goalie_lineups is None
    assert c.game_fee_share == pytest.approx(0.40) and c.game_lineups is None
    assert c.cap_status["GOALIE_CAP"].startswith("DOLLARS 0.40")


def test_too_few_goalies_relax_the_lineup_cap_by_the_smallest_step_and_say_so():
    c = exposure.caps(CFG, 5, classic_pool(), Mode.CLASSIC, 4, fees_cents=[25, 25, 100, 10, 10], usable_goalies=2,
                      budget=BUDGET["classic"])
    assert c.goalie_lineups == 3  # ceil(5 / 2): two goalies cannot hold five entries at two each
    assert "relaxed from 2: 2 usable goalie(s)" in c.cap_status["GOALIE_CAP"]
    assert any("GOALIE_CAP relaxed from 2 to 3 lineups" in n for n in c.notes)


def _pool_with_games():
    from dataclasses import replace

    from pool_builder import make_pool

    info = {"AAA": "AAA@BBB 10/15/2026 07:00PM ET", "BBB": "AAA@BBB 10/15/2026 07:00PM ET",
            "CCC": "CCC@DDD 10/15/2026 09:00PM ET"}
    return make_pool(Mode.CLASSIC, [replace(r, game_info=info[r.team]) for r in classic_pool().rows])


def test_fee_shares_primary_game_and_shared_failure():
    pool = _pool_with_games()
    ids = [r.role_id for r in pool.rows]
    rng = np.random.default_rng(0)
    base = rng.integers(0, 60, size=(500, len(ids))).astype(np.int32)
    scen = ob.ScenarioSet(ids, base, "selection", 1)
    lu = pick_legal(pool)
    assignment = {"e1": lu, "e2": lu, "e3": lu}
    fees = {"e1": 300, "e2": 100, "e3": 100}
    pays = {"e1": np.zeros(500, np.int64), "e2": np.ones(500, np.int64), "e3": (np.arange(500) % 2).astype(np.int64)}
    conc = exposure.concentration(assignment, pool, fees, scen, pays)
    g = exposure.goalies_in(lu, pool)[0]
    assert conc["goalie"] == {g: 1.0}
    pg = exposure.primary_game(lu, scen.means_tenths(), exposure.game_of(pool), pool)
    assert conc["game"] == {pg: 1.0}
    sf = conc["shared_failure"]
    assert sf["baseline_fee_share_cashing_nothing"] == pytest.approx((300 + 0 + 100 * 0.5) / 500)
    assert sf["worst"] and all(0 <= w["fee_share_cashing_nothing"] <= 1 for w in sf["worst"])


from lineup_helpers import pick_legal  # noqa: E402


def test_usable_goalies_are_counted_relative_to_each_team():
    from types import SimpleNamespace

    from pool_builder import make_pool, row

    rows, persons, n = [], {}, 0
    for i in range(8):
        team = f"T{i}"
        for p_start in (0.6, 0.2, 0.2):
            n += 1
            r = row(n, team, "G", 8000)
            rows.append(r)
            persons[r.person_key] = SimpleNamespace(goalie=SimpleNamespace(p_start=p_start))
    pool = make_pool(Mode.CLASSIC, rows)
    assert exposure.usable_goalies(SimpleNamespace(persons=persons), pool) == 8  # every team's starter, no camp backups
    for k, p in persons.items():  # a camp split (0.45, 0.30, 0.25): all three are within half of the leader
        p.goalie.p_start = {0: 0.45, 1: 0.30, 2: 0.25}[(int(k.split("|")[0][1:]) - 1) % 3]
    assert exposure.usable_goalies(SimpleNamespace(persons=persons), pool) == 24
