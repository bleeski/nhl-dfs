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


def test_fee_budget_below_the_achievable_floor_uses_the_floor():
    pool = classic_pool()
    c = exposure.caps(CFG, 2, pool, Mode.CLASSIC, 4, fees_cents=[100, 100], usable_goalies=6, budget=BUDGET["classic"])
    assert c.goalie_fee_share == pytest.approx(0.5) and c.floors["goalie_fee_share_max"] == 0.5
    assert exposure.fee_floor([2000, 100, 100], 3) == pytest.approx(2000 / 2200)
    assert exposure.fee_floor([100] * 10, 5) == pytest.approx(0.2)


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
