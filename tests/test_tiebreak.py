"""C8 deterministic tie-break: ownership decides only inside one band, never past it."""

import random

import pytest

from nhl_dfs.build import objectives as ob
from nhl_dfs.build import tiebreak as tb
from nhl_dfs.contracts.statuses import FieldCalibration

pytestmark = pytest.mark.c8

CFG = ob.load_risk_config()


def test_prefer_uses_ownership_only_inside_the_band():
    # within the band: lower ownership wins even with a slightly lower metric
    assert tb.prefer(1.00, 0.99, own_a=150, own_b=90, dup_a=1, dup_b=1, band=0.02) == "b"
    # outside the band: the better metric wins whatever the ownership
    assert tb.prefer(1.00, 0.97, own_a=150, own_b=10, dup_a=9, dup_b=0, band=0.02) == "a"
    # equal ownership: lower duplicate risk decides
    assert tb.prefer(1.0, 1.0, 100, 100, dup_a=3, dup_b=2, band=0.01) == "b"
    # cash policy ignores ownership inside the band
    assert tb.prefer(1.00, 0.99, own_a=150, own_b=10, dup_a=9, dup_b=0, band=0.02, policy="mean") == "a"


def test_band_is_never_smaller_than_the_monte_carlo_se():
    assert tb.band_width(10.0, 0.01, CFG) == pytest.approx(0.3)  # 3% governs
    assert tb.band_width(10.0, 2.0, CFG) == pytest.approx(2.0)  # the SE floor governs


def test_rank_never_prefers_a_candidate_more_than_one_band_worse():
    rng = random.Random(7)
    for trial in range(200):
        items = [tb.Item(f"k{i}", rng.uniform(0, 1), rng.uniform(0, 0.05), rng.uniform(0, 200), rng.uniform(-30, 0))
                 for i in range(rng.randint(2, 25))]
        for policy in tb.POLICIES:
            ranked = tb.rank(items, CFG, policy)
            assert sorted(r.item.key for r in ranked) == sorted(i.key for i in items)
            for i, hi in enumerate(ranked):
                for lo in ranked[i + 1:]:
                    # anything placed later is never more than one band (of the earlier item's band) better
                    assert lo.item.value <= hi.anchor, (trial, policy)
                    if lo.band_index == hi.band_index:
                        assert hi.item.value >= hi.anchor - hi.band - 1e-12


def test_rank_is_deterministic_and_orders_ownership_within_a_band():
    items = [tb.Item("a", 1.00, 0.0, 120.0, 0.0), tb.Item("b", 0.995, 0.0, 60.0, 0.0), tb.Item("c", 0.5, 0.0, 1.0, 0.0)]
    order = [r.item.key for r in tb.rank(items, CFG, "own_then_dup")]
    assert order == ["b", "a", "c"]
    assert [r.item.key for r in tb.rank(items, CFG, "mean")] == ["a", "b", "c"]
    assert order == [r.item.key for r in tb.rank(list(reversed(items)), CFG, "own_then_dup")]


def _pool():
    from pool_builder import classic_pool

    return classic_pool()


def test_dup_measure_proxy_before_fit_and_sampled_after():
    pool = _pool()
    ids = tuple(r.role_id for r in pool.rows[:9])
    v, which = tb.dup_measure(ids, "K", {}, {"K": 4.5}, FieldCalibration.PRIOR, pool)
    assert which == "proxy"
    v2, which2 = tb.dup_measure(ids, "K", {}, {"K": 4.5}, FieldCalibration.FITTED, pool)
    assert which2 == "sampled" and v2 == 4.5
