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


def test_select_does_not_depend_on_the_thread_count():
    import copy

    pool = classic_pool()
    lus = disjoint_lineups(pool, 3)
    a, b, f = lus
    S = 1500
    rng = np.random.default_rng(12)
    fld = rng.integers(300, 700, S)
    scen = scenarios_for(pool, [a, b, f], [fld + rng.integers(-60, 80, S), fld + rng.integers(-40, 60, S), fld])
    ct = {"G": contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37), "C": contest("C", "cash", 10, [180] * 4)}
    fields = {"G": ob.FieldSpec([f.role_ids], [f.key], np.asarray([1]), 197, "weighted", np.asarray([197], np.int64)),
              "C": one_field(f)}
    entries = [E("e1", "G"), E("e2", "G"), E("e3", "C")]
    caps = exposure.caps(EXPO, 3, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    out = []
    for threads in (1, 5):
        cfg = copy.deepcopy(RISK)
        cfg["selection"]["threads"] = threads
        sel = pf.select([a, b], scen, fields, ct, entries, caps, pf.RiskBudget(0.9, None, None, None), seed=1, pool=pool,
                        fam_cfg=FAM, risk_cfg=cfg)
        out.append((sel.by_entry, sel.chosen_kappa, [p.record() for p in sel.frontier]))
    assert out[0] == out[1]


def _goalie_candidates(pool, per_goalie):
    """per_goalie distinct legal lineups for each goalie (candidates.generate with one forced goalie per family)."""
    from nhl_dfs.build import candidates as cand_mod
    from nhl_dfs.build.milp import GroupConstraint

    goalies = sorted((r for r in pool.rows if r.is_goalie), key=lambda r: r.team)
    menu = [(f"goalie:{g.person_key}", (GroupConstraint(frozenset({g.role_id}), min_count=1),)) for g in goalies]
    obj = {r.role_id: 1.0 for r in pool.rows}
    got = cand_mod.generate(pool, Mode.CLASSIC, obj, per_goalie * len(goalies), seed=3, perturb_sd=1.0, groups_menu=menu)
    return goalies, [Candidate(c.role_ids, c.key, 0.0, c.family) for c in got]


def _goalie_scenarios(pool, goalies, edge):
    """Each lineup's score sits on its goalie's column: goalie i scores the field plus edge[i]."""
    ids = [r.role_id for r in pool.rows]
    col = {r: i for i, r in enumerate(ids)}
    S = 1000
    fld = np.random.default_rng(5).integers(300, 700, S)
    base = np.zeros((S, len(ids)), np.int32)
    for g, e in zip(goalies, edge):
        base[:, col[g.role_id]] = fld + e
    return ob.ScenarioSet(ids, base, "selection", 1), fld


def _select_goalies(cands, goalies, usable, worst):
    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    scen, _ = _goalie_scenarios(pool, goalies, [80, 30, 20, 10])  # goalie AAA is clearly the best
    fees = {"e1": 25, "e2": 25, "e3": 100, "e4": 10, "e5": 10}  # 2026-10-01: $1.70, the $1 entry is 59%
    ct = {"G": contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37)}
    field = ob.FieldSpec([worst.role_ids], [worst.key], np.asarray([1]), 195, "weighted", np.asarray([195], np.int64))
    caps = exposure.caps(EXPO, 5, pool, Mode.CLASSIC, 1, fees_cents=list(fees.values()), usable_goalies=usable,
                         budget=RISK["budget"]["classic"])
    budget = pf.RiskBudget(1.0, caps.goalie_fee_share, None, None, caps.goalie_lineups, None)
    sel = pf.select(cands, scen, {"G": field}, ct, [E(e, "G") for e in fees], caps, budget, seed=1, pool=pool,
                    fam_cfg=FAM, risk_cfg=RISK, fees_cents=fees)
    return pool, caps, sel


def test_unequal_fees_cap_each_goalie_at_two_of_five_lineups():
    """B36 acceptance: 2026-10-01 fees and four usable goalies; the best goalie may hold only 2 of 5 entries."""
    from collections import Counter

    from nhl_dfs.build.scenario_pass import risk_statuses

    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    goalies, cands = _goalie_candidates(pool, 3)
    worst = next(c for c in cands if goalies[3].role_id in c.role_ids)
    _, caps, sel = _select_goalies(cands, goalies, 4, worst)
    n = Counter(g for lu in sel.by_entry.values() for g in exposure.goalies_in(lu, pool))
    assert max(n.values()) <= 2 and n[goalies[0].person_key] == 2  # the best goalie still gets its two
    assert not any(r.kind == "CONCENTRATION" for r in sel.relaxations)
    st = risk_statuses(sel, caps)
    assert st["GOALIE_CAP"].startswith("LINEUPS 2/5") and st["RISK_BUDGET"] == "OK"


def test_candidates_short_of_goalies_relax_by_one_lineup_and_report_a_breach():
    """The caps believe four goalies are usable, but the candidates hold only two: the fifth entry relaxes the
    cap by the smallest step (one lineup), recorded as CONCENTRATION, and RISK_BUDGET says BREACHED."""
    from collections import Counter

    from nhl_dfs.build.scenario_pass import risk_statuses

    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    goalies, cands = _goalie_candidates(pool, 3)
    two = {goalies[0].role_id, goalies[1].role_id}
    held = [c for c in cands if two & set(c.role_ids)]
    worst = next(c for c in cands if goalies[3].role_id in c.role_ids)  # the field's lineup, not a candidate
    _, caps, sel = _select_goalies(held, goalies, 4, worst)
    n = Counter(g for lu in sel.by_entry.values() for g in exposure.goalies_in(lu, pool))
    conc = [r for r in sel.relaxations if r.kind == "CONCENTRATION"]
    assert sorted(n.values()) == [2, 3]  # five entries on two goalies: one goes over by exactly one
    assert len(conc) == 1 and "exceeded by 1 lineup" in conc[0].detail
    assert not any(r.kind == "REPEAT" for r in sel.relaxations)
    st = risk_statuses(sel, caps)
    assert st["RISK_BUDGET"].startswith("BREACHED(") and "relaxed the goalie/game cap" in st["RISK_BUDGET"]


def test_discovery_forces_candidates_for_every_usable_goalie():
    """B36: discovery adds per_goalie lineups holding each usable goalie, even when one goalie dominates."""
    from collections import Counter

    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    goalies = sorted((r for r in pool.rows if r.is_goalie), key=lambda r: r.team)
    design, _ = _goalie_scenarios(pool, goalies, [400, 0, 0, 0])  # goalie AAA is far ahead
    runtime = {"candidates": {"perturb_sd_points": 0.5, "time_limit_total_s": 20.0, "min_pairwise_diff": 2}}
    keys = [g.person_key for g in goalies[1:]]  # AAA is the one the central search finds anyway
    got, report = pf.discover(pool, design, 6, runtime, RISK, seed=1, goalies=keys)
    fam = Counter(c.family for c in got if c.family.startswith("goalie:"))
    per = int(RISK["discovery"]["per_goalie"])
    assert {f"goalie:{k}" for k in keys} == set(fam) and all(v == per for v in fam.values())
    for c in got:
        if c.family.startswith("goalie:"):
            assert c.family.split(":", 1)[1] in exposure.goalies_in(c.role_ids, pool)
    assert report["target"]["goalie"] == per * len(keys)
