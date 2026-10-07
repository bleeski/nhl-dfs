"""C18 joint own-entry accounting (backlog B55 and B56; reviews R04 and R05).

The selection fill used to add each new entry's prize to a running total without taking away what the entry
takes from entries already placed in the same contest. `objectives.OwnContest` replaces that. These tests pin
it against three recounts that do not share its code path: a from-scratch recount through the same kernel
(`OwnContest.totals`, which uses `own_pairwise`), the production `joint_payouts`, and an integer reference
written here from the DraftKings rules (tied places pooled and divided, floored to the cent; flag 9).

Tolerances are the ones fixed in BUILD_STATUS flag 37 before any result: payout exact (0 cents) everywhere;
utility exact for large_gpp, small_field and cash; for wta and satellite at most 1 cent per entry-scenario cell
against the float32 `joint_payouts` and the integer reference (the kernel's float64 share differs from them in
rare tie cells; that difference already existed and is kept).
"""

from types import SimpleNamespace

import numpy as np
import pytest

from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.statuses import PayoutSource

pytestmark = pytest.mark.c18

CFG = ob.load_risk_config()
TOP_PCT = float(CFG["objectives"]["top_pct"])
FAMILIES = ("large_gpp", "small_field", "wta", "cash", "satellite")
EXACT_UTILITY = {"large_gpp", "small_field", "cash"}


# -- fixtures -----------------------------------------------------------------------------------------------------

def contest_of(family, field_size, prizes, n_seats=0, face=None):
    p = np.asarray(prizes, np.int64)
    seats = np.zeros(len(p), bool)
    seats[:n_seats] = True
    return ob.Contest("c", "c", family, field_size, 100, p, seats, face, PayoutSource.PRIOR)


def make_case(family, seed, dtype, S=60, F=5, K=7, M=6):
    """Small random scenario fixture with a narrow score range, so ties between own entries and the field are common."""
    rng = np.random.default_rng(1000 * seed + FAMILIES.index(family))
    fs = rng.integers(0, 5, (S, F))
    w = rng.integers(1, 5, F).astype(np.int64)
    cand = rng.integers(0, 5, (S, K))
    paid = int(rng.integers(3, 9))
    prizes = np.sort(rng.integers(100, 5000, paid))[::-1]
    ct = contest_of(family, int(w.sum()) + M, prizes, n_seats=3 if family == "satellite" else 0,
                    face=1000 if family == "satellite" else None)
    G, E = ob.ranks(cand, fs, w)
    return SimpleNamespace(ct=ct, fs=fs, w=w, cand=cand, G=G.astype(dtype), E=E.astype(dtype), S=S, K=K, M=M, rng=rng,
                           curve=ob.PayCurve(ct, TOP_PCT))


def production_totals(case, cols):
    """(payout, utility) cents per scenario summed over own entries `cols`, by the production joint_payouts."""
    if not cols:
        return np.zeros(case.S, np.int64), np.zeros(case.S, np.int64)
    m = ob.joint_payouts(case.cand[:, cols], case.fs, case.w, case.ct, cfg=CFG)
    return m.payout_cents.astype(np.int64).sum(1), m.utility_cents.astype(np.int64).sum(1)


def reference_totals(ct, fs, w, own):
    """The DraftKings rules in plain integers, one scenario at a time. Shares no code with objectives.py ranking."""
    S, J = own.shape
    prizes = [int(x) for x in ct.prizes_cents]
    seats = [bool(x) for x in ct.seats]
    top_k = max(1, int(TOP_PCT * ct.field_size))
    face = ct.ticket_face_cents or 0
    pay_t, util_t = np.zeros(S, np.int64), np.zeros(S, np.int64)
    for s in range(S):
        comps = [(int(v), int(n), None) for v, n in zip(fs[s], w)] + [(int(v), 1, j) for j, v in enumerate(own[s])]
        place = 0
        for v in sorted({c[0] for c in comps}, reverse=True):
            group = [c for c in comps if c[0] == v]
            n = sum(c[1] for c in group)
            share = sum(prizes[place:place + n]) // n
            if ct.family == "large_gpp":
                util = sum(prizes[place:min(place + n, top_k)]) // n
            elif ct.family == "wta":
                util = prizes[0] // n if place == 0 else 0
            elif ct.family == "satellite":
                util = sum(seats[place:place + n]) * face // n + share
            else:
                util = share
            mine = sum(1 for c in group if c[2] is not None)
            pay_t[s] += mine * share
            util_t[s] += mine * util
            place += n
    return pay_t, util_t


def check(family, got, want, n, label):
    """got, want = (payout, utility) per scenario; n = how many entry-cells the totals sum over."""
    assert np.array_equal(got[0], want[0]), f"{label}: payout differs"
    if family in EXACT_UTILITY:
        assert np.array_equal(got[1], want[1]), f"{label}: utility differs"
    else:
        gap = np.abs(got[1] - want[1])
        assert gap.max() <= n, f"{label}: utility differs by {gap.max()} cents (> {n})"


# -- the primitive against the recounts --------------------------------------------------------------------------

@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("dtype", [np.int16, np.int32])
@pytest.mark.parametrize("skip", [False, True], ids=["all-pairs", "paid-region-only"])
@pytest.mark.parametrize("seed", range(4))
def test_increments_and_running_totals_equal_the_recounts(family, dtype, seed, skip):
    case = make_case(family, seed, dtype)
    S, K = case.S, case.K
    oc = ob.OwnContest(case.curve.pay_util, case.G, case.E, case.cand, capacity=2, chunk=23,  # growth and chunking exercised
                       live_below=case.ct.paid if skip else None)
    picks = [int(k) for k in case.rng.integers(0, K, case.M)]  # repeats allowed: the same lineup entered twice
    run = [np.zeros(S, np.int64), np.zeros(S, np.int64)]
    cols: list[int] = []
    pool = int(case.ct.prizes_cents.sum())
    for step, k in enumerate(picks):
        full = oc.marginal(0, S)
        parts = [oc.marginal(a, min(S, a + 17)) for a in range(0, S, 17)]
        for i in (0, 1):  # row chunks of any size give the same increments
            assert np.array_equal(np.concatenate([p[i] for p in parts]), full[i])
        base = production_totals(case, cols)
        for kk in range(K):  # every candidate: increment == joint(placed + [kk]) - joint(placed)
            after = production_totals(case, cols + [kk])
            check(family, (full[0][:, kk], full[1][:, kk]), (after[0] - base[0], after[1] - base[1]),
                  2 * len(cols) + 1, f"step {step} candidate {kk} vs joint_payouts")
        ref_base = reference_totals(case.ct, case.fs, case.w, case.cand[:, cols])
        for kk in {k, (k + 1) % K}:
            ref_after = reference_totals(case.ct, case.fs, case.w, case.cand[:, cols + [kk]])
            check(family, (full[0][:, kk], full[1][:, kk]), (ref_after[0] - ref_base[0], ref_after[1] - ref_base[1]),
                  2 * len(cols) + 1, f"step {step} candidate {kk} vs the integer reference")
        d = oc.add(k)
        for i in (0, 1):
            assert np.array_equal(d[i], full[i][:, k]), f"step {step}: add() differs from marginal()"
            run[i] = run[i] + d[i]
        cols.append(k)
        own = oc.totals()
        assert np.array_equal(own[0], run[0]) and np.array_equal(own[1], run[1]), f"step {step}: totals() differs"
        check(family, tuple(run), production_totals(case, cols), len(cols), f"step {step} running vs joint_payouts")
        check(family, tuple(run), reference_totals(case.ct, case.fs, case.w, case.cand[:, cols]), len(cols),
              f"step {step} running vs the integer reference")
        assert int(run[0].max()) <= pool, f"step {step}: own cash {int(run[0].max())} exceeds the prize pool {pool}"


def test_several_contests_keep_separate_books():
    first = make_case("large_gpp", 7, np.int16)  # the same candidates in every contest, different fields and curves
    cases = [first] + [make_case_with(make_case(f, 7, np.int16), first.cand) for f in ("cash", "wta")]
    ocs = [ob.OwnContest(c.curve.pay_util, c.G, c.E, c.cand, live_below=c.ct.paid) for c in cases]
    slate = np.zeros(cases[0].S, np.int64)
    placed = [[], [], []]
    for step, (j, k) in enumerate([(0, 1), (1, 1), (0, 3), (2, 3), (0, 3), (1, 0), (2, 5), (0, 6)]):
        slate = slate + ocs[j].add(k)[0]
        placed[j].append(k)
        want = sum(production_totals(cases[i], placed[i])[0] for i in range(3))
        assert np.array_equal(slate, want), f"step {step}: the slate total differs from the sum of the contests"


def make_case_with(case, cand):
    """The same case rebuilt around a shared candidate score matrix."""
    G, E = ob.ranks(cand, case.fs, case.w)
    return SimpleNamespace(**{**case.__dict__, "cand": cand, "G": G.astype(np.int16), "E": E.astype(np.int16)})


# -- worked examples ---------------------------------------------------------------------------------------------

def _tie_example(order):
    """Prizes 1000, 600, 400, 200; a field lineup x3 scoring 5; own X = 7, Y = 5, Z = 5 (so 3 + 3 = 6 entries)."""
    ct = contest_of("small_field", 6, [1000, 600, 400, 200])
    cand = np.array([[7, 5, 5]])
    G, E = ob.ranks(cand, np.array([[5]]), np.array([3], np.int64))
    oc = ob.OwnContest(ob.PayCurve(ct, TOP_PCT).pay_util, G.astype(np.int16), E.astype(np.int16), cand, live_below=ct.paid)
    out = []
    for k in order:
        marg = oc.marginal(0, 1)[0]
        out.append(([int(marg[0, j]) for j in range(3)], int(oc.add(k)[0][0])))
    return out


def test_hand_worked_ties_and_overtakes():
    # Alone, X takes 1st (1000). Y and Z each sit in a tie group with the 3 field lineups.
    # Order X, Y, Z: Y given X is 4 sharing places 2..5 = 1200 / 4 = 300; Z then makes it 5 sharing places 2..6
    # = 1200 / 5 = 240 each, so Z's own 240 plus Y's loss of 60 is +180. Total 1000 + 300 + 180 = 1480.
    first = _tie_example([0, 1, 2])
    assert [d for _, d in first] == [1000, 300, 180]
    assert first[0][0] == [1000, 550, 550] and first[1][0][2] == 300 and first[2][0][2] == 180
    # Order Y, X (a strict overtake): Y alone shares 4 places = 2200 / 4 = 550; X above it adds 1000 but Y drops to
    # 300, so +750; Z last is +180 again. The portfolio is the same 1480 either way.
    second = _tie_example([1, 0, 2])
    assert [d for _, d in second] == [550, 750, 180]
    assert sum(d for _, d in first) == sum(d for _, d in second) == 1480


def test_review_r04_counterexample_at_the_primitive_level():
    """100-entry winner-take-all, $1 entry, $90 first prize, 98 opponent copies scoring 5; four equally likely
    scenario types repeated 1,000 times: A, B, C = (10, 11, 0), (10, 11, 0), (10, 0, 0), (0, 0, 10)."""
    rows = np.array([[10, 11, 0], [10, 11, 0], [10, 0, 0], [0, 0, 10]])
    cand = np.tile(rows, (1000, 1))
    S = cand.shape[0]
    ct = contest_of("wta", 100, [9000])
    G, E = ob.ranks(cand, np.full((S, 1), 5), np.array([98], np.int64))
    curve = ob.PayCurve(ct, TOP_PCT)
    oc = ob.OwnContest(curve.pay_util, G.astype(np.int16), E.astype(np.int16), cand, live_below=ct.paid)
    alone = oc.marginal(0, S)
    assert [alone[1][:, k].mean() / 100 for k in range(3)] == [67.5, 45.0, 22.5]  # A, then B, then C
    oc.add(0)  # A is placed
    after_a = oc.marginal(0, S)
    assert after_a[1][:, 1].mean() == 0.0  # B only moves the win from A to B: nothing gained
    assert after_a[1][:, 2].mean() / 100 == 22.5  # C wins where A does not
    assert (after_a[0][:, 1] == 0).all()  # B wins 9000 where A loses 9000: the payout does not move in any scenario
    ab = oc.total[0] + after_a[0][:, 1]
    ac = oc.total[0] + after_a[0][:, 2]
    assert ab.mean() / 100 == 67.5 and (ab == 0).mean() == 0.25  # A+B: $67.50, a quarter of scenarios pay nothing
    assert ac.mean() / 100 == 90.0 and (ac == 0).mean() == 0.0  # A+C: $90.00, never nothing
    assert int(ab.max()) <= 9000  # one prize, never two


# -- a portfolio with one entry in every contest must not move (flag 37) -------------------------------------------

def golden_fixture(seed):
    """Three contests (large_gpp, wta, cash), one entry each, three candidates, a spare-role field lineup per contest."""
    from nhl_dfs.build import exposure, portfolio as pf
    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.models import contests as contests_mod
    from pool_builder import classic_pool
    from test_portfolio import E, contest, disjoint_lineups, scenarios_for

    pool = classic_pool()
    cands = disjoint_lineups(pool, 3)
    used = {r for lu in cands for r in lu.role_ids}
    spare = [SimpleNamespace(role_ids=(r.role_id,)) for r in pool.rows if r.role_id not in used][:3]
    S = 1500
    rng = np.random.default_rng(seed)
    base = rng.integers(300, 700, S)
    totals = [base + rng.integers(-80, 90, S), base + rng.integers(-50, 130, S), base + rng.integers(-120, 60, S)]
    fld = [rng.integers(300, 700, S) for _ in range(3)]
    scen = scenarios_for(pool, cands + spare, totals + fld)
    cts = {"G": contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37),
           "W": contest("W", "wta", 12, [1200]),
           "C": contest("C", "cash", 10, [180] * 4)}
    n_opp = {"G": 199, "W": 11, "C": 9}
    fields = {cid: ob.FieldSpec([sp.role_ids], [f"spare{i}"], np.asarray([1]), n_opp[cid], "weighted",
                                np.asarray([n_opp[cid]], np.int64))
              for i, (cid, sp) in enumerate(zip(cts, spare))}
    entries = [E("e1", "G"), E("e2", "W"), E("e3", "C")]
    risk = ob.load_risk_config()
    fam = contests_mod.load_contest_families()
    caps = exposure.caps(exposure.load_exposure_config(), 3, pool, Mode.CLASSIC, 1, budget=risk["budget"]["classic"])

    def run(kappa):
        import copy

        cfg = copy.deepcopy(risk)
        cfg["frontier"]["kappas"] = [kappa]
        sel = pf.select(cands, scen, fields, cts, entries, caps, pf.RiskBudget(1.0, None, None, None), seed=1, pool=pool,
                        fam_cfg=fam, risk_cfg=cfg)
        return {e: (ch.cand, ch.score, ch.score_se, ch.band_index) for e, ch in sel.choices.items()}, sel.frontier[0].record()

    return run, [float(k) for k in risk["frontier"]["kappas"]]


# Captured from the UNCHANGED selection fill (the commit before the joint accounting reached it), at 12 decimals.
# Keys: (fixture seed, kappa). Values: per entry (candidate index, score, score SE, band index) and the frontier figures.
GOLDEN = {
    (31, 0.0): ({'e1': (2, 23.167833333333, 0.643731434842, 0), 'e2': (0, 6.377333333333, 0.154581052933, 0), 'e3': (1, 1.06152, 0.022830355372, 0)},
               {'tail_utility': 10.20223, 'p_lose80': 0.204, 'exp_payout': 30.60809}),
    (31, 0.25): ({'e1': (2, 23.014833333333, 0.643778730779, 0), 'e2': (0, 6.178833333333, 0.15481693628, 0), 'e3': (1, 0.75502, 0.024735540417, 0)},
                {'tail_utility': 10.20223, 'p_lose80': 0.204, 'exp_payout': 30.60809}),
    (31, 0.5): ({'e1': (2, 22.861833333333, 0.643920597748, 0), 'e2': (0, 5.980333333333, 0.155522439729, 0), 'e3': (1, 0.44852, 0.029727301588, 0)},
               {'tail_utility': 10.20223, 'p_lose80': 0.204, 'exp_payout': 30.60809}),
    (31, 1.0): ({'e1': (2, 22.555833333333, 0.644487753339, 0), 'e2': (0, 5.583333333333, 0.158313016708, 0), 'e3': (1, -0.16448, 0.044397910533, 0)},
               {'tail_utility': 10.20223, 'p_lose80': 0.204, 'exp_payout': 30.60809}),
    (31, 2.0): ({'e1': (2, 21.943833333333, 0.646751402166, 0), 'e2': (0, 4.789333333333, 0.169015204225, 0), 'e3': (1, -1.39048, 0.079504858089, 0)},
               {'tail_utility': 10.20223, 'p_lose80': 0.204, 'exp_payout': 30.60809}),
    (77, 0.0): ({'e1': (2, 20.134033333333, 0.633133800182, 0), 'e2': (0, 6.052, 0.154818810227, 0), 'e3': (1, 1.04088, 0.022943908786, 0)},
               {'tail_utility': 9.07564, 'p_lose80': 0.236, 'exp_payout': 27.22775}),
    (77, 0.25): ({'e1': (2, 19.957033333333, 0.633187194227, 0), 'e2': (0, 5.835, 0.155067634706, 0), 'e3': (1, 0.72488, 0.024856634602, 0)},
                {'tail_utility': 9.07564, 'p_lose80': 0.236, 'exp_payout': 27.22775}),
    (77, 0.5): ({'e1': (2, 19.780033333333, 0.633347349353, 0), 'e2': (0, 5.618, 0.155811723992, 0), 'e3': (1, 0.40888, 0.029868717477, 0)},
               {'tail_utility': 9.07564, 'p_lose80': 0.236, 'exp_payout': 27.22775}),
    (77, 1.0): ({'e1': (2, 19.426033333333, 0.633987565282, 0), 'e2': (0, 5.184, 0.158753208891, 0), 'e3': (1, -0.22312, 0.044601482977, 0)},
               {'tail_utility': 9.07564, 'p_lose80': 0.236, 'exp_payout': 27.22775}),
    (77, 2.0): ({'e1': (2, 18.718033333333, 0.636541989921, 0), 'e2': (0, 4.316, 0.170010980038, 0), 'e3': (1, -1.48712, 0.079861757329, 0)},
               {'tail_utility': 9.07564, 'p_lose80': 0.236, 'exp_payout': 27.22775}),
}


@pytest.mark.parametrize("seed", [31, 77])
def test_a_portfolio_with_one_entry_in_every_contest_is_unchanged(seed):
    """Flag 37: the same candidate per entry and knob, score and SE within 1e-9 of the unchanged fill's, same frontier."""
    run, kappas = golden_fixture(seed)
    for kappa in kappas:
        choices, record = run(kappa)
        want_choices, want_frontier = GOLDEN[(seed, kappa)]
        assert {e: c[0] for e, c in choices.items()} == {e: c[0] for e, c in want_choices.items()}, f"kappa {kappa}"
        for e, (cand, score, se, band) in choices.items():
            w = want_choices[e]
            assert (cand, band) == (w[0], w[3]) and score == pytest.approx(w[1], abs=1e-9) and se == pytest.approx(w[2], abs=1e-9), \
                f"kappa {kappa} entry {e}"
        for name, value in want_frontier.items():
            assert record[name] == pytest.approx(value, abs=1e-9), f"kappa {kappa} {name}"
