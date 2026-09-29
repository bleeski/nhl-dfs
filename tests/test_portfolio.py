"""C8 portfolio: allocation by family objective, tie-break policy, frontier, least-risk fallback."""

from dataclasses import dataclass

import numpy as np
import pytest

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build.candidates import Candidate
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, Mode, check_lineup, lineup_key, slot_accepts
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.models import contests as contests_mod
from pool_builder import classic_pool

pytestmark = pytest.mark.c8

RISK = ob.load_risk_config()
FAM = contests_mod.load_contest_families()
EXPO = exposure.load_exposure_config()


@dataclass
class E:
    entry_id: str
    contest_id: str


def disjoint_lineups(pool, n):
    """n legal Classic lineups that share no person (classic_pool is roomy enough for three)."""
    free = list(pool.rows)
    out = []
    for i in range(n):
        lu, teams = [], []
        for slot in CLASSIC_SLOTS:
            pick = None
            for r in sorted(free, key=lambda r: (r.team in teams[:2] and slot != "G", r.role_id)):
                if slot_accepts(slot, r, Mode.CLASSIC) and r.person_key not in {x.person_key for x in lu}:
                    pick = r
                    break
            lu.append(pick)
            free.remove(pick)
            if not pick.is_goalie:
                teams.append(pick.team)
        assert check_lineup(lu, Mode.CLASSIC).ok
        out.append(Candidate(tuple(r.role_id for r in lu), lineup_key(lu, Mode.CLASSIC), 0.0, "central"))
    return out


def scenarios_for(pool, lineups, totals):
    """Put each lineup's whole scenario score (tenths) on its first role; everything else 0."""
    ids = [r.role_id for r in pool.rows]
    col = {r: i for i, r in enumerate(ids)}
    S = len(totals[0])
    base = np.zeros((S, len(ids)), np.int32)
    for lu, t in zip(lineups, totals):
        base[:, col[lu.role_ids[0]]] = t
    return ob.ScenarioSet(ids, base, "selection", 1)


def contest(cid, family, field_size, prizes, fee=100):
    p = np.asarray(prizes, np.int64)
    return ob.Contest(cid, cid, family, field_size, fee, p, np.zeros(len(p), bool), None, PayoutSource.PRIOR)


def one_field(lineup):
    return ob.FieldSpec([lineup.role_ids], [lineup.key], np.asarray([1]), 1, "weighted", np.asarray([1], np.int64))


def test_wta_allocation_prefers_first_place_equity_over_variance():
    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    S = 4000
    rng = np.random.default_rng(2)
    fld = rng.integers(400, 600, S)
    steady = fld + np.where(rng.random(S) < 0.65, 5, -5)  # beats the opponent 65% of the time, by a little
    wild = np.where(rng.random(S) < 0.45, fld + 400, fld - 300)  # higher variance and mean, wins 45%
    assert wild.std() > steady.std() and wild.mean() > steady.mean()
    scen = scenarios_for(pool, [a, b, f], [steady, wild, fld])
    ct = {"W": contest("W", "wta", 2, [180])}
    caps = exposure.caps(EXPO, 1, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    sel = pf.select([a, b], scen, {"W": one_field(f)}, ct, [E("e1", "W")], caps,
                    pf.RiskBudget(1.0, None, None, None), seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK)
    assert sel.by_entry["e1"] == a.role_ids
    assert sel.portfolio.per_entry["e1"]["first_place_equity"] == pytest.approx(0.65, abs=0.03)


def test_cash_objective_ignores_ownership_and_gpp_uses_it_inside_the_band():
    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    S = 2000
    rng = np.random.default_rng(4)
    fld = rng.integers(300, 700, S)
    same = fld + 1  # a and b score identically: every metric ties exactly
    scen = scenarios_for(pool, [a, b, f], [same, same.copy(), fld])
    caps = exposure.caps(EXPO, 1, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    budget = pf.RiskBudget(1.0, None, None, None)

    def pick(family, own_a, own_b):
        own = {a.role_ids[0]: own_a, b.role_ids[0]: own_b}
        ct = {"C": contest("C", family, 2, [180])}
        sel = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, budget, seed=1, pool=pool,
                        fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own})
        return sel.by_entry["e1"]

    assert pick("cash", 90.0, 5.0) == pick("cash", 5.0, 90.0)  # ownership does not move a cash choice
    assert pick("large_gpp", 90.0, 5.0) == b.role_ids and pick("large_gpp", 5.0, 90.0) == a.role_ids


def test_frontier_report_has_no_dominated_points_and_infeasible_budget_takes_least_risk():
    P = pf.FrontierPoint
    pts = [P(0.0, 1.0, 0.1, 0.9, 0.01, 5, 0.5, None, None, False, reasons=["risk"]),
           P(0.5, 0.9, 0.1, 0.7, 0.01, 5, 0.5, None, None, False, reasons=["risk"]),
           P(1.0, 0.8, 0.1, 0.8, 0.01, 5, 0.5, None, None, False, reasons=["risk"]),  # dominated by kappa 0.5
           P(2.0, 0.5, 0.1, 0.65, 0.01, 5, 0.5, None, None, False, reasons=["risk"])]
    pf.mark_dominated(pts)
    report = pf.frontier_report(pts)
    assert [p.kappa for p in report] == [2.0, 0.5, 0.0]
    for p in report:
        assert not any(q.tail_utility >= p.tail_utility and q.p_lose80 <= p.p_lose80 and
                       (q.tail_utility > p.tail_utility or q.p_lose80 < p.p_lose80) for q in pts)
    best, why = pf.choose(pts)
    assert best.kappa == 2.0 and "least-risk" in why
    pts[1].feasible = True
    best, why = pf.choose(pts)
    assert best.kappa == 0.5 and "inside the risk budget" in why


def test_select_respects_caps_and_counts_own_entries_as_opponents():
    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    S = 1000
    rng = np.random.default_rng(9)
    fld = rng.integers(300, 700, S)
    scen = scenarios_for(pool, [a, b, f], [fld + 50, fld + 40, fld])
    ct = {"G": contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37)}
    field = ob.FieldSpec([f.role_ids], [f.key], np.asarray([1]), 198, "weighted", np.asarray([198], np.int64))
    caps = exposure.caps(EXPO, 2, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    sel = pf.select([a, b], scen, {"G": field}, ct, [E("e1", "G"), E("e2", "G")], caps,
                    pf.RiskBudget(1.0, None, None, None), seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK)
    assert {sel.by_entry["e1"], sel.by_entry["e2"]} == {a.role_ids, b.role_ids}  # no repeat while b is available
    pe = sel.portfolio.per_entry
    top = max(pe, key=lambda e: pe[e]["exp_payout"])
    assert pe[top]["exp_payout"] == pytest.approx(50.0)  # always first
    other = next(e for e in pe if e != top)
    assert pe[other]["exp_payout"] == pytest.approx(20.0)  # always second, behind its own sibling
