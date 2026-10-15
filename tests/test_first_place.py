"""C55 (BEN flags 107 to 112, docs/experiments/first_place_2026-10-15.md): first-place equity as the goal of large_gpp and
small_field entries, the washout goal unchanged.

With `objectives.goal: first_place` a large_gpp or small_field entry's utility in a scenario is the first prize split evenly
among the entries tied for first, rounded down to the cent, when nothing scores above it, else 0 (`objectives.first_place_cents`).
`_evaluate` and `PayCurve.pay_util` share that integer kernel, so selection and the reported figures agree to the cent; own entries
in one contest are counted together through C18's `OwnContest`. wta, cash, satellite and top_n keep their goals. A config without
the key is origin/master's top1pct goal. Tolerances are flag 37's: exact for large_gpp, small_field, cash and top_n; at most 1 cent
per entry-scenario cell for wta and satellite, whose float share is unchanged. No network.
"""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.statuses import PayoutSource

pytestmark = pytest.mark.c55

BASE = ob.load_risk_config()
TOP = ob.with_goal(BASE, "top1pct")
FIRST = ob.with_goal(BASE, "first_place")
ABSENT = {**BASE, "objectives": {k: v for k, v in BASE["objectives"].items() if k != "goal"}}  # origin/master's config
TOP_PCT = float(BASE["objectives"]["top_pct"])
FAMILIES = ("large_gpp", "small_field", "wta", "cash", "satellite", "top_n")
EXACT = {"large_gpp", "small_field", "cash", "top_n"}
UNCHANGED = ("wta", "cash", "satellite", "top_n")


def contest_of(family, field_size, prizes, n_seats=0, face=None):
    p = np.asarray(prizes, np.int64)
    seats = np.zeros(len(p), bool)
    seats[:n_seats] = True
    return ob.Contest("c", "c", family, field_size, 100, p, seats, face, PayoutSource.PRIOR)


def make_case(family, seed, S=80, F=5, K=7, M=6):
    """Random scores in a narrow range, so ties at the top (field and own) are common."""
    rng = np.random.default_rng(500 + 10 * seed + FAMILIES.index(family))
    fs = rng.integers(0, 6, (S, F))
    w = rng.integers(1, 4, F).astype(np.int64)
    cand = rng.integers(0, 6, (S, K))
    paid = int(rng.integers(3, 9))
    prizes = np.sort(rng.integers(100, 5000, paid))[::-1]
    ct = contest_of(family, int(w.sum()) + M, prizes, n_seats=3 if family == "satellite" else 0,
                    face=1000 if family == "satellite" else None)
    G, E = ob.ranks(cand, fs, w)
    return SimpleNamespace(ct=ct, fs=fs, w=w, cand=cand, G=G, E=E, S=S, K=K)


def reference_first(ct, fs, w, own):
    """First-place dollars per scenario summed over own entries, from the DraftKings rule in plain integers (no objectives.py
    ranking): the group at the top shares the first prize evenly, rounded down; every other place gets nothing."""
    S, J = own.shape
    first = int(ct.prizes_cents[0]) if ct.paid else 0
    out = np.zeros(S, np.int64)
    for s in range(S):
        comps = [(int(v), int(n), False) for v, n in zip(fs[s], w)] + [(int(v), 1, True) for v in own[s]]
        top = max(c[0] for c in comps)
        group = [c for c in comps if c[0] == top]
        n = sum(c[1] for c in group)
        out[s] = sum(1 for c in group if c[2]) * (first // n)
    return out


# -- (1) the two utility paths agree --------------------------------------------------------------------------------

@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("cfg", [TOP, FIRST, ABSENT], ids=["top1pct", "first_place", "no-key"])
@pytest.mark.parametrize("seed", range(3))
def test_evaluate_and_pay_util_give_the_same_cents(family, cfg, seed):
    c = make_case(family, seed)
    m = ob.metrics_from_ranks(c.G, c.E, c.ct, cfg)
    pay, util = ob.PayCurve(c.ct, TOP_PCT, ob.goal_of(cfg)).pay_util(c.G, c.E)
    assert np.array_equal(m.payout_cents, pay)
    gap = np.abs(m.utility_cents.astype(np.int64) - util)
    assert gap.max() <= (0 if family in EXACT else 1)
    if ob.goal_of(cfg) == "first_place" and family in ob.FIRST_PLACE_FAMILIES:
        first = int(c.ct.prizes_cents[0])
        want = np.where(c.G == 0, first // (c.E + 1), 0)
        assert np.array_equal(m.utility_cents, want) and np.array_equal(util, want)
        assert m.objective_name == "first_place_equity"
        assert np.allclose(m.objective, m.first_place_equity)


# -- (2) own entries tied for first split the prize; C18's recounts still match --------------------------------------

def test_two_own_entries_tied_for_first_each_get_half_floored():
    S = 10
    ct = contest_of("large_gpp", 100, [1001, 500, 300])  # an odd first prize: 1001 // 2 = 500 each, never 1001 twice
    own = np.column_stack([np.full(S, 50), np.full(S, 50)])
    fs, w = np.full((S, 1), 40), np.array([98], np.int64)
    m = ob.joint_payouts(own, fs, w, ct, cfg=FIRST)
    assert (m.utility_cents == 500).all()
    assert (m.utility_cents.sum(axis=1) <= 1001).all()
    assert (m.payout_cents == (1001 + 500) // 2).all()  # the payout still pools places 1 and 2 (flag 9)
    curve = ob.PayCurve(ct, TOP_PCT, "first_place")
    G, E = ob.ranks(own, fs, w)
    oc = ob.OwnContest(curve.pay_util, G, E, own, capacity=2)
    oc.add(0)
    d = oc.add(1)
    assert (oc.total[1] == 1000).all() and (d[1] == -1).all()  # the second takes half of the first's prize; the floor costs a cent


@pytest.mark.parametrize("family", ob.FIRST_PLACE_FAMILIES)
@pytest.mark.parametrize("dtype", [np.int16, np.int32])
@pytest.mark.parametrize("seed", range(4))
def test_the_joint_fill_under_first_place_equals_every_recount(family, dtype, seed):
    c = make_case(family, seed)
    G, E = c.G.astype(dtype), c.E.astype(dtype)
    curve = ob.PayCurve(c.ct, TOP_PCT, "first_place")
    oc = ob.OwnContest(curve.pay_util, G, E, c.cand, capacity=3, chunk=17, live_below=c.ct.paid)
    order = [3, 0, 5, 1]
    for k in order:
        marg = oc.marginal(0, c.S)
        before = oc.total[1].copy() if oc.total is not None else np.zeros(c.S, np.int64)
        oc.add(k)
        assert np.array_equal(marg[1][:, k], oc.total[1] - before)
    want = reference_first(c.ct, c.fs, c.w, c.cand[:, order])
    assert np.array_equal(oc.total[1], want)
    assert np.array_equal(oc.totals()[1], want)
    m = ob.joint_payouts(c.cand[:, order], c.fs, c.w, c.ct, cfg=FIRST)
    assert np.array_equal(m.utility_cents.astype(np.int64).sum(axis=1), want)


# -- (3) the other families do not move; top1pct is origin/master's -----------------------------------------------------

@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(3))
def test_only_large_gpp_and_small_field_change_with_the_goal(family, seed):
    c = make_case(family, seed)
    top, first, absent = (ob.metrics_from_ranks(c.G, c.E, c.ct, cfg) for cfg in (TOP, FIRST, ABSENT))
    for name in ("payout_cents", "utility_cents", "objective", "objective_se", "top_payout_cents", "first_cents"):
        assert np.array_equal(getattr(top, name), getattr(absent, name)), name
    assert top.objective_name == absent.objective_name == ob.FAMILY_OBJECTIVE[family]
    if family in UNCHANGED:
        for name in ("payout_cents", "utility_cents", "objective", "objective_se"):
            assert np.array_equal(getattr(top, name), getattr(first, name)), name
        assert first.objective_name == top.objective_name
    for goal in ob.GOALS:
        a = ob.PayCurve(c.ct, TOP_PCT, goal).pay_util(c.G, c.E)
        b = ob.PayCurve(c.ct, TOP_PCT).pay_util(c.G, c.E)
        assert np.array_equal(a[0], b[0])
        if goal == "top1pct" or family in UNCHANGED:
            assert np.array_equal(a[1], b[1])
    assert ob.family_objective(FIRST)[family] == ("first_place_equity" if family in ob.FIRST_PLACE_FAMILIES
                                                  else ob.FAMILY_OBJECTIVE[family])


def test_the_record_keeps_the_top1pct_figures_under_first_place():
    c = make_case("large_gpp", 0)
    m = ob.metrics_from_ranks(c.G, c.E, c.ct, FIRST)
    row = m.row(0)
    assert row["objective"] == "first_place_equity"
    assert row["exp_payout_top1pct"] == round(float(m.exp_payout_top1pct[0]), 4) and "p_top1pct" in row
    assert np.array_equal(m.top_payout_cents, ob.metrics_from_ranks(c.G, c.E, c.ct, TOP).top_payout_cents)


def test_a_bad_goal_is_refused():
    bad = copy.deepcopy(BASE)
    bad["objectives"]["goal"] = "top_1_percent"
    with pytest.raises(ValueError, match="objectives.goal"):
        ob.validate_risk_config(bad)
    with pytest.raises(ValueError):
        ob.with_goal(BASE, "first")


# -- (4) no prize, no NaN; (5) the PRIOR remainder is in the first prize ----------------------------------------------

def test_no_paid_places_or_a_zero_first_prize_gives_zero_and_no_nan():
    rng = np.random.default_rng(9)
    cand, fs, w = rng.integers(0, 5, (40, 3)), rng.integers(0, 5, (40, 2)), np.array([2, 3], np.int64)
    for prizes in ([], [0, 0, 0]):
        ct = contest_of("large_gpp", 10, prizes)
        m = ob.contest_metrics(cand, fs, w, ct, cfg=FIRST)
        assert (m.utility_cents == 0).all() and not np.isnan(m.objective).any() and not np.isnan(m.objective_se).any()
        G, E = ob.ranks(cand, fs, w)
        assert (ob.PayCurve(ct, TOP_PCT, "first_place").pay_util(G, E)[1] == 0).all()


def test_the_frontier_chooses_deterministically_when_every_utility_is_zero():
    from nhl_dfs.build import portfolio as pf

    pts = [pf.FrontierPoint(k, 0.0, 0.0, 0.3, 0.01, 0.0, 0.2, None, None, True) for k in (0.0, 0.25, 0.5, 1.0, 2.0)]
    picks = {pf.choose(list(order))[0].kappa for order in (pts, pts[::-1], pts[2:] + pts[:2])}
    assert picks == {0.0}


def test_the_prior_power_curves_rounding_remainder_is_inside_the_first_prize():
    from nhl_dfs.models import contests as contests_mod

    fam = contests_mod.load_contest_families()
    prizes, seats, face, _ = ob.prior_curve("large_gpp", 5000, 100, fam)
    pool = int(np.floor(100 * 5000 * (1.0 - float(fam["payout_priors"]["rake"]))))
    assert int(prizes.sum()) == pool  # the remainder went somewhere inside the table: _power puts it on first place
    ct = ob.Contest("p", "p", "large_gpp", 5000, 100, prizes, seats, face, PayoutSource.PRIOR)
    alone, tied = ob.PayCurve(ct, TOP_PCT, "first_place").pay_util(np.zeros((1, 2), np.int64), np.array([[0, 2]]))[1][0]
    assert alone == int(prizes[0]) and tied == int(prizes[0]) // 3


# -- (6) a small field drawn per scenario; (7) Showdown's twentieths ----------------------------------------------------

def test_a_sampled_small_field_counts_first_only_when_every_drawn_opponent_is_lower():
    S = 6
    distinct = np.array([[10, 90]] * S)  # lineup 1 scores 90 in every scenario but is drawn only in scenarios 3 to 5
    draws = np.array([[0, 0, 0]] * 3 + [[0, 1, 0]] * 3, np.int32)
    field = ob.SampledScores(distinct, draws)
    cand = np.full((S, 1), 50)
    ct = contest_of("small_field", 4, [600, 300])
    m = ob.contest_metrics(cand, field, np.ones(3, np.int64), ct, cfg=FIRST)
    assert m.utility_cents[:, 0].tolist() == [600, 600, 600, 0, 0, 0]
    assert m.first_place_equity[0] == pytest.approx(0.5)


def test_showdown_first_place_reads_the_captain_twentieths():
    from nhl_dfs.contracts.geometry import Mode

    roles = ["a", "b", "c"]
    base = np.array([[10, 4, 0], [10, 4, 0]], np.int32)  # tenths
    scen = ob.ScenarioSet(roles, base, "selection", 1)
    cand = scen.scores([["a", "b"], ["b", "a"]], Mode.SHOWDOWN).full()  # captain first: 3x10 + 2x4 = 38, 3x4 + 2x10 = 32
    field = scen.scores([["c", "a"]], Mode.SHOWDOWN).full()  # 3x0 + 2x10 = 20
    assert cand.tolist() == [[38, 32], [38, 32]] and field.tolist() == [[20], [20]]
    ct = contest_of("large_gpp", 4, [700, 200])
    m = ob.contest_metrics(cand, field, np.ones(1, np.int64), ct, own_copies=None, cfg=FIRST)
    assert m.utility_cents.tolist() == [[700, 700], [700, 700]]  # each alone against the field
    j = ob.joint_payouts(cand, field, np.ones(1, np.int64), ct, cfg=FIRST)
    assert j.utility_cents.tolist() == [[700, 0], [700, 0]]  # together: the Captain on a beats the Captain on b


# -- (8) switch off: origin/master's choices and published file -------------------------------------------------------

@pytest.mark.parametrize("seed", [31, 77])
@pytest.mark.parametrize("cfg", [TOP, ABSENT], ids=["top1pct", "no-key"])
def test_switch_off_selection_matches_the_golden_values_of_the_unchanged_fill(seed, cfg):
    """Flag 37's golden fixture (large_gpp, wta, cash) and values, captured before C18 from the unchanged fill."""
    from test_joint_accounting import GOLDEN, golden_fixture

    run, kappas = golden_fixture(seed, risk=cfg)
    for kappa in kappas:
        choices, record = run(kappa)
        want_choices, want_frontier = GOLDEN[(seed, kappa)]
        for e, (cand, score, se, band) in choices.items():
            w = want_choices[e]
            assert (cand, band) == (w[0], w[3]) and score == pytest.approx(w[1], abs=1e-9) and se == pytest.approx(w[2], abs=1e-9)
        for name, value in want_frontier.items():
            assert record[name] == pytest.approx(value, abs=1e-9)


@pytest.fixture(scope="module")
def mini_runs(tmp_path_factory):
    """The mini Classic slate through the whole scenario pass under each config (the run reads ob.load_risk_config)."""
    from conftest import mini_pair
    from nhl_dfs.build.run import run_slate

    small = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}
    out = {}
    real = ob.load_risk_config
    try:
        for name, cfg in (("top1pct", TOP), ("no-key", ABSENT), ("first_place", FIRST)):
            ob.load_risk_config = lambda path=ob.RISK_YAML, cfg=cfg: copy.deepcopy(cfg)
            tmp = tmp_path_factory.mktemp(f"c55_{name}")
            r = run_slate(*mini_pair("classic"), offline=True, baseline_only=False, scenario=True, scenario_n=small,
                          out_root=tmp / "runs", outputs_root=tmp / "outputs",
                          clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
            assert r.ok, r.manifest["failed"]
            files = sorted((tmp / "outputs").rglob("DKEntries.csv"))
            out[name] = (r, files[-1].read_bytes() if files else None)
    finally:
        ob.load_risk_config = real
    return out


def test_switch_off_publishes_the_same_bytes_with_and_without_the_key(mini_runs):
    a, b = mini_runs["top1pct"][1], mini_runs["no-key"][1]
    assert a is not None and a == b
    assert mini_runs["top1pct"][0].manifest["scenario"]["goal"] == "top1pct"


def test_first_place_runs_end_to_end_and_records_its_goal(mini_runs):
    r, data = mini_runs["first_place"]
    sec = r.manifest["scenario"]
    assert data is not None and sec["goal"] == "first_place"
    gpp = [e for e in sec["entries"] if e["family"] in ob.FIRST_PLACE_FAMILIES]
    assert gpp and all(e["objective"] == "first_place_equity" and "exp_payout_top1pct" in e for e in gpp)
    assert "top1pct_utility" in sec["portfolio"] and all("top1pct_utility" in p for p in sec["frontier"])


# -- (9) late swap reads the goal, per path ---------------------------------------------------------------------------

def _repair_fixture(cfg):
    """A large_gpp contest of 200 where candidate A finishes second in half the scenarios and never first, and candidate B
    finishes first in a tenth of them and nowhere else: the top-1% goal (places 1 and 2) prefers A, first place prefers B."""
    from nhl_dfs.build import swap_objective as so_mod
    from nhl_dfs.contracts.geometry import Mode
    from nhl_dfs.contracts.statuses import FieldCalibration
    from nhl_dfs.models import contests as contests_mod
    from pool_builder import classic_pool
    from test_portfolio import contest, disjoint_lineups, scenarios_for

    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    used = {r for lu in (a, b, f) for r in lu.role_ids}
    g = SimpleNamespace(role_ids=(next(r.role_id for r in pool.rows if r.role_id not in used),))
    S = 2000
    s = np.arange(S)
    totals = [np.where(s % 2 == 0, 75, 0), np.where(s % 10 == 0, 120, 0), np.full(S, 100), np.full(S, 50)]
    scen = scenarios_for(pool, [a, b, f, g], totals)
    ct = contest("G", "large_gpp", 200, [5000, 2000, 1000] + [200] * 37)
    field = np.hstack([scen.scores([f.role_ids], Mode.CLASSIC).full(), scen.scores([g.role_ids], Mode.CLASSIC).full()])
    obj = object.__new__(so_mod.ScenarioObjective)
    obj.sets, obj.mode, obj.pool, obj.risk_cfg, obj.live = {"selection": scen}, Mode.CLASSIC, pool, cfg, None
    obj.fam_cfg = contests_mod.load_contest_families()
    obj.field_cal = FieldCalibration.PRIOR
    obj.cache = SimpleNamespace(contests={"G": ct}, own_by={}, dup_by={}, contest_family={"G": "large_gpp"})
    sorted_field = so_mod.SortedField(field, np.array([1, 198], np.int64))
    obj._field = lambda cid, purpose: (sorted_field, None)
    return obj, [list(a.role_ids), list(b.role_ids)]


def test_late_swap_repairs_by_first_place_when_the_goal_is_first_place():
    picks = {}
    for name, cfg in (("top1pct", TOP), ("first_place", FIRST)):
        obj, cands = _repair_fixture(cfg)
        pick = obj.choose("e1", "G", cands, [])
        picks[name] = (pick.index, pick.figures["objective"])
    assert picks["top1pct"][0] == 0 and picks["first_place"][0] == 1
    assert picks["first_place"][1] == "first_place_equity" and picks["top1pct"][1] == "exp_payout_top1pct"


def test_each_late_swap_path_keeps_top1pct_where_its_key_says_so():
    from nhl_dfs.build.swap_objective import path_goal

    rt = {"late_swap": {"fast_goal": "top1pct", "full_goal": None}}
    cfg, g = path_goal(FIRST, rt, fast=True)
    assert ob.goal_of(cfg) == "top1pct" and g["goal"] == "top1pct" and "fast_goal" in g["why"]
    cfg, g = path_goal(FIRST, rt, fast=False)
    assert cfg is FIRST and g["goal"] == "first_place"
    cfg, g = path_goal(TOP, rt, fast=True)
    assert cfg is TOP and g["goal"] == "top1pct"
    with pytest.raises(ValueError):
        path_goal(FIRST, {"late_swap": {"fast_goal": "first"}}, fast=True)


def test_resolve_builds_the_fast_objective_with_its_paths_goal_and_says_so(monkeypatch, tmp_path):
    from nhl_dfs.build import notes, swap_objective as so_mod

    seen = {}

    class Fake:
        def __init__(self, *a, risk_cfg=None, **kw):
            seen["goal"] = ob.goal_of(risk_cfg)
            self.linear, self.sets = {}, {}
            self.record = {"cache_run": "r", "n": {"selection": 4000, "referee": 4000}, "games_resimulated": [],
                           "games_changed_but_started": []}

    monkeypatch.setattr(so_mod.scache, "find", lambda root, run_id: (SimpleNamespace(), []))
    monkeypatch.setattr(so_mod, "ScenarioObjective", Fake)
    monkeypatch.setattr(so_mod, "build_role_model", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no roles")))
    monkeypatch.setattr(ob, "load_risk_config", lambda path=ob.RISK_YAML: copy.deepcopy(FIRST))
    rt = {"late_swap": {"fast_goal": "top1pct", "full_goal": None}}
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    kw = dict(pool=None, work=None, st={}, started_games=set(), dk_rec=None, runs_root=tmp_path, run_id="r", offline=True,
              cache=None, clock=lambda: now, now=now, runtime=rt, optional_ok=(True, ""))
    res = so_mod.resolve("scenario", fast=True, **kw)
    assert res.kind == "scenario" and seen["goal"] == "top1pct" and res.goal["goal"] == "top1pct"
    res2 = so_mod.resolve("scenario", fast=False, **kw)
    assert seen["goal"] == "first_place" and res2.goal["goal"] == "first_place"
    lines = notes._objective_lines({"objective": res.record()})
    assert any(line.startswith("- GOAL=top1pct") and "fast_goal" in line for line in lines)


# -- (10) the QA packet and the controller follow the goal --------------------------------------------------------------

def test_the_qa_packet_names_first_place_and_keeps_the_top1pct_figure():
    from nhl_dfs.build import packet
    from nhl_dfs.models import contests as contests_mod

    qa, fam = packet.load_qa_config(), contests_mod.load_contest_families()
    first = packet.acceptance(FIRST, qa, fam)
    assert first["goal"] == "first_place" and first["tail_metric"] == "first_place_equity"
    assert first["family_objective"]["large_gpp"] == first["family_objective"]["small_field"] == "first_place_equity"
    assert "exp_payout_top1pct" in first["information_metrics"]
    top, absent = packet.acceptance(TOP, qa, fam), packet.acceptance(ABSENT, qa, fam)
    assert top == absent and top["tail_metric"] == "exp_payout_top1pct" and "goal" not in top
    assert top["family_objective"] == ob.FAMILY_OBJECTIVE


def _sample(goal):
    from nhl_dfs.build import packet

    rows = [{"entry_id": "e1", "family": "large_gpp", "value": 0.9, "first_place_equity": 0.001, "exp_payout_top1pct": 0.9},
            {"entry_id": "e2", "family": "large_gpp", "value": 0.2, "first_place_equity": 0.004, "exp_payout_top1pct": 0.2}]
    row = SimpleNamespace(team="AAA", is_goalie=False, name="x", salary=3000)
    v = SimpleNamespace(m={"scenario": {"goal": goal, "entries": rows}}, lineups={"e1": ["r1"], "e2": ["r2"]},
                        row=lambda r: row, slots=["C"], contest_of={"e1": "G", "e2": "G"})
    return packet._sample(v, 1, {})[0]


def test_the_packet_sample_reads_the_goals_figure():
    assert _sample("first_place")["entry_id"] == "e2" and _sample("first_place")["why"] == "highest first-place equity"
    assert _sample(None)["entry_id"] == "e1" and _sample(None)["why"] == "highest tail"


def test_the_controller_tail_is_first_place_dollars_under_first_place():
    from nhl_dfs.build import controller
    from nhl_dfs.contracts.geometry import Mode

    rng = np.random.default_rng(5)
    base = rng.integers(200, 800, (4000, 2))
    util = {"before": base, "after": base + np.array([300, 0])}  # a clear utility gain on the edited entry
    top = {"before": base, "after": base + np.array([0, -30])}  # the sibling's top-1% payout falls 0.30 everywhere
    first = {"before": base, "after": base}  # its first-place dollars do not

    def joint(cid, lineups, purpose):
        side = "after" if lineups[0][0] == "NEW" else "before"
        return SimpleNamespace(payout_cents=np.full(base.shape, 500), utility_cents=util[side], top_payout_cents=top[side],
                               first_cents=first[side])

    so = SimpleNamespace(cache=SimpleNamespace(contests={"c": object()}), joint=joint)
    before = {"e0": ["OLD"], "e1": ["OLD"]}
    after = {**before, "e0": ["NEW"]}
    qa = {"budget": {"classic": {"p_lose80_max": 0.6}}, "tiebreak": {"band_pct": 0.03, "se_mult": 1.0}}
    args = ({e: "c" for e in before}, {e: 1.0 for e in before})
    ok_top, figs_top = controller.evaluate(so, before, after, {"e0"}, *args, qa, Mode.CLASSIC)
    ok_first, figs_first = controller.evaluate(so, before, after, {"e0"}, *args, {**qa, "objectives": {"goal": "first_place"}},
                                               Mode.CLASSIC)
    assert not ok_top and not figs_top["selection"]["tail_ok"]  # origin/master's rule: the top-1% loss is caught
    sel = figs_first["selection"]
    assert ok_first and sel["tail_ok"] and sel["tail_metric"] == "first_place" and sel["tail_gain"] == 0.0
    assert sel["top1pct_after"] - sel["top1pct_before"] == pytest.approx(-0.30, abs=1e-4)  # still reported
