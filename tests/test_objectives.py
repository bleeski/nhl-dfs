"""C8 scenario objectives: exact DK tie pooling, weighted field ranks, family metrics."""

import json
from decimal import Decimal

import numpy as np
import pytest

from conftest import fixture_bytes
from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.data.sources import dk_public
from nhl_dfs.models import contests as contests_mod

pytestmark = pytest.mark.c8

CFG = ob.load_risk_config()
FAM = contests_mod.load_contest_families()


def contest(prizes_dollars, field_size, family="large_gpp", seats=None, face=None, fee=1):
    prizes = np.asarray([ob.to_cents(Decimal(str(p))) for p in prizes_dollars], np.int64)
    s = np.zeros(len(prizes), bool) if seats is None else np.asarray(seats, bool)
    return ob.Contest("c1", "test", family, field_size, fee * 100, prizes, s, face, PayoutSource.PRIOR)


def col(*xs):
    return np.asarray(xs, np.int32).reshape(1, -1)


def test_split_tie_decimal_reference_rounds_down_to_the_cent():
    assert ob.split_tie([Decimal("100"), Decimal("50"), Decimal("25.01")], 3) == Decimal("58.33")
    assert ob.split_tie([Decimal("0.10"), Decimal("0.10"), Decimal("0.05")], 3) == Decimal("0.08")
    assert ob.split_tie([Decimal("1")], 1) == Decimal("1.00")


def test_three_way_tie_for_first_splits_pooled_cash_rounded_down():
    c = contest([100, 50, 25.01, 10], field_size=20)
    field = col(500, 500, 300, 200)  # two opponents tie the candidate at 500
    m = ob.contest_metrics(col(500), field, np.ones(4, np.int64), c, cfg=CFG)
    want = ob.split_tie([Decimal("100"), Decimal("50"), Decimal("25.01")], 3)
    assert int(m.payout_cents[0, 0]) == ob.to_cents(want) == 5833
    assert m.first_place_equity[0] == pytest.approx(1 / 3)


def test_integer_floor_equals_decimal_round_down_on_random_ties():
    rng = np.random.default_rng(3)
    for _ in range(200):
        prizes = rng.integers(1, 100000, size=8)
        prizes = np.sort(prizes)[::-1]
        c = ob.Contest("c", "t", "large_gpp", 30, 100, prizes.astype(np.int64), np.zeros(8, bool), None, PayoutSource.PRIOR)
        g = int(rng.integers(0, 6))
        t = int(rng.integers(1, 5))
        field = col(*([900] * g + [500] * (t - 1) + [100] * 5))
        m = ob.contest_metrics(col(500), field, np.ones(field.shape[1], np.int64), c, cfg=CFG)
        ref = ob.split_tie([Decimal(int(x)) / 100 for x in prizes[g:g + t]], t)
        assert int(m.payout_cents[0, 0]) == ob.to_cents(ref)


def test_own_copies_count_as_copies():
    c = contest([100, 50, 25], field_size=20)
    field = col(500, 100)
    m = ob.contest_metrics(col(500), field, np.ones(2, np.int64), c, own_copies=col(500), cfg=CFG)
    assert int(m.payout_cents[0, 0]) == 5833  # own copy + field entry + itself: (100 + 50 + 25) / 3
    m2 = ob.contest_metrics(col(500), field, np.ones(2, np.int64), c, own_copies=col(600), cfg=CFG)
    assert int(m2.payout_cents[0, 0]) == 3750  # own entry above: positions 2..3 shared with the field's 500


def test_weighted_field_ranks_equal_explicit_expansion():
    rng = np.random.default_rng(11)
    S, F, K = 40, 7, 5
    field = rng.integers(0, 12, size=(S, F)).astype(np.int32) * 10
    weights = rng.integers(1, 5, size=F).astype(np.int64)
    cand = rng.integers(0, 12, size=(S, K)).astype(np.int32) * 10
    expanded = np.repeat(field, weights, axis=1)
    n = int(weights.sum()) + 1
    prizes = sorted(rng.integers(1, 5000, size=n // 2), reverse=True)
    c = ob.Contest("c", "t", "large_gpp", n, 100, np.asarray(prizes, np.int64), np.zeros(len(prizes), bool), None,
                   PayoutSource.PRIOR)
    a = ob.contest_metrics(cand, field, weights, c, cfg=CFG)
    b = ob.contest_metrics(cand, expanded, np.ones(expanded.shape[1], np.int64), c, cfg=CFG)
    assert np.array_equal(a.payout_cents, b.payout_cents)
    assert np.allclose(a.p_top1pct, b.p_top1pct) and np.allclose(a.p_clear_line, b.p_clear_line)


def test_chunking_does_not_change_results():
    rng = np.random.default_rng(5)
    field = rng.integers(0, 50, size=(300, 20)).astype(np.int32)
    cand = rng.integers(0, 50, size=(300, 6)).astype(np.int32)
    c = contest([50, 20, 10, 5, 5, 2, 2, 2], field_size=21)
    a = ob.contest_metrics(cand, field, np.ones(20, np.int64), c, cfg=CFG)
    tiny = {**CFG, "objectives": {**CFG["objectives"], "memory_cap_mb": 0.0001}}
    b = ob.contest_metrics(cand, field, np.ones(20, np.int64), c, cfg=tiny)
    assert np.array_equal(a.payout_cents, b.payout_cents) and np.array_equal(a.utility_cents, b.utility_cents)


def test_cash_line_is_tie_adjusted():
    c = contest([1.8] * 4, field_size=10, family="cash")  # 4 paid of 10
    field = col(900, 900, 900, 500, 500, 100, 100, 100, 100)
    m = ob.contest_metrics(col(500), field, np.ones(9, np.int64), c, cfg=CFG)
    assert m.objective_name == "p_clear_line"
    assert m.p_clear_line[0] == pytest.approx(1 / 3)  # positions 4..6, one of three inside the line
    assert int(m.payout_cents[0, 0]) == 180 // 3


def test_satellite_objective_counts_seats_and_keeps_the_ticket_apart():
    c = contest([0, 0, 0], field_size=10, family="satellite", seats=[True, True, True], face=2500)
    field = col(900, 900, 500, 100)
    m = ob.contest_metrics(col(500), field, np.ones(4, np.int64), c, cfg=CFG)
    assert m.objective_name == "p_seat"
    assert m.p_seat[0] == pytest.approx(0.5)  # positions 3..4 share one seat
    assert int(m.payout_cents[0, 0]) == 0  # a ticket is not cash
    assert m.exp_ticket_value[0] == pytest.approx(12.5)


def test_top1pct_uses_the_field_size_and_wta_equity():
    c = contest([500] + [10] * 99, field_size=1000)
    rng = np.random.default_rng(1)
    field = rng.integers(0, 1000, size=(200, 50)).astype(np.int32)
    w = np.full(50, 999 // 50, np.int64)
    w[: 999 - int(w.sum())] += 1
    m = ob.contest_metrics(np.full((200, 1), 990, np.int32), field, w, c, cfg=CFG)
    assert m.top_k == 10 and 0 <= m.p_top1pct[0] <= 1
    wta = contest([9], field_size=10, family="wta")
    m2 = ob.contest_metrics(col(700), col(700, 100), np.ones(2, np.int64), wta, cfg=CFG)
    assert m2.first_place_equity[0] == pytest.approx(0.5) and int(m2.payout_cents[0, 0]) == 450


def test_joint_payouts_rank_own_entries_against_each_other():
    c = contest([100, 50, 25], field_size=5)
    own = np.asarray([[600, 500]], np.int32)
    m = ob.joint_payouts(own, col(550, 100), np.ones(2, np.int64), c, cfg=CFG)
    assert list(m.payout_cents[0]) == [10000, 2500]  # 1st, and 3rd behind the field's 550


def test_exact_curve_from_a_recorded_contest_detail_is_per_position():
    for name, total in (("dk_contest_195958173.json", "1000"), ("dk_contest_196048725.json", "150")):
        d = dk_public.parse_contest_detail(json.loads(fixture_bytes(name)))
        prizes, seats, face, _ = ob.exact_curve(d)
        assert int(prizes.sum()) == ob.to_cents(Decimal(total))
        assert not seats.any() and face is None
        assert all(prizes[i] >= prizes[i + 1] for i in range(len(prizes) - 1))


def test_prior_curves_sum_to_the_pool_and_end_at_min_cash():
    prizes, seats, face, detail = ob.prior_curve("large_gpp", 5000, 100, FAM)
    pool = int(5000 * 100 * (1 - FAM["payout_priors"]["rake"]))
    assert int(prizes.sum()) == pool and len(prizes) == 1000 and prizes[-1] == 200
    assert all(prizes[i] >= prizes[i + 1] for i in range(len(prizes) - 1))
    cash, _, _, _ = ob.prior_curve("cash", 200, 100, FAM)
    assert len(set(cash.tolist())) == 1 and len(cash) == 90
    wta, _, _, _ = ob.prior_curve("wta", 10, 500, FAM)
    assert len(wta) == 1 and int(wta[0]) == int(10 * 500 * (1 - FAM["payout_priors"]["rake"]))
    sat, seats, face, _ = ob.prior_curve("satellite", 200, 100, FAM)
    assert seats.all() and not sat.any() and face == int(200 * 100 * 0.85) // 20


def test_field_spec_weighted_and_sampled_modes():
    lineups = [("a",), ("b",), ("a",), ("c",), ("a",)]
    keys = ["A", "B", "A", "C", "A"]
    big = ob.field_spec(lineups, keys, 4999, CFG, n_scenarios=10, seed=1, salt="x")
    assert big.mode == "weighted" and int(big.weights.sum()) == 4999 and big.keys == ["A", "B", "C"]
    small = ob.field_spec(lineups, keys, 9, CFG, n_scenarios=10, seed=1, salt="x")
    again = ob.field_spec(lineups, keys, 9, CFG, n_scenarios=10, seed=1, salt="x")
    assert small.mode == "sampled" and small.draws.shape == (10, 9) and np.array_equal(small.draws, again.draws)


def test_play_mask_is_seeded_and_near_its_rate():
    keys = ["a", "b", "c"]
    m1 = ob.play_mask(20000, keys, {"b": 0.85}, seed=4, purpose_code=1)
    m2 = ob.play_mask(20000, keys, {"b": 0.85}, seed=4, purpose_code=1)
    assert np.array_equal(m1, m2) and m1[:, 0].all() and m1[:, 2].all()
    assert abs(m1[:, 1].mean() - 0.85) < 0.01
    assert not np.array_equal(m1, ob.play_mask(20000, keys, {"b": 0.85}, seed=4, purpose_code=2))


def test_contests_sharing_one_field_match_separate_evaluation():
    from pool_builder import classic_pool

    from lineup_helpers import pick_legal

    pool = classic_pool()
    ids = [r.role_id for r in pool.rows]
    rng = np.random.default_rng(8)
    scen = ob.ScenarioSet(ids, rng.integers(0, 40, size=(300, len(ids))).astype(np.int32), "referee", 1)
    lu = pick_legal(pool)
    alt = tuple(reversed(lu))  # same people: a copy of the same lineup, which ties with it
    flds = [tuple(ids[i:i + 9]) for i in range(0, 27, 3)]
    keys = [f"k{i}" for i in range(len(flds))]
    a = ob.field_spec(flds, keys, 1500, CFG, n_scenarios=300, seed=1, salt="a")
    b = ob.field_spec(flds, keys, 1498, CFG, n_scenarios=300, seed=1, salt="b")
    ca = ob.Contest("A", "A", "large_gpp", 1501, 100, np.arange(300, 0, -1, dtype=np.int64) * 10, np.zeros(300, bool), None,
                    PayoutSource.PRIOR)
    cb = ob.Contest("B", "B", "large_gpp", 1500, 100, np.arange(300, 0, -1, dtype=np.int64) * 7, np.zeros(300, bool), None,
                    PayoutSource.PRIOR)
    assignment = {"e1": lu, "e2": lu, "e3": alt}
    ev = {"A": ob.ContestEval(ca, a, ["e1"]), "B": ob.ContestEval(cb, b, ["e2", "e3"])}
    grouped = ob.joint_by_contest(assignment, ev, scen, pool=pool, cfg=CFG)
    for cid, spec, ct, eids in (("A", a, ca, ["e1"]), ("B", b, cb, ["e2", "e3"])):
        own = scen.scores([assignment[e] for e in eids], pool.mode).full()
        fs, w = spec.scores(scen, pool.mode)
        sep = ob.joint_payouts(own, fs, w, ct, cfg=CFG)
        assert grouped[cid][0] == eids
        assert np.array_equal(grouped[cid][1].payout_cents, sep.payout_cents)
