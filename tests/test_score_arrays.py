"""C6: vectorized scoring equals contracts.scoring; the Captain multiplier applies exactly once."""

import numpy as np
import pytest

from nhl_dfs.contracts import scoring as sc
from nhl_dfs.sim import outcomes as oc
from nhl_dfs.sim import score

pytestmark = pytest.mark.c6


def _random_outcomes(rng, n=1000, p=6):
    is_g = np.array([False] * (p - 2) + [True, True])
    o = oc.empty([f"k{i}" for i in range(p)], is_g, n)
    sk = ~is_g
    k = int(sk.sum())
    o.goals[:, sk] = rng.poisson(0.6, (n, k))
    o.assists[:, sk] = rng.poisson(0.8, (n, k))
    o.sog[:, sk] = o.goals[:, sk] + rng.poisson(2.5, (n, k))
    o.blocks[:, sk] = rng.poisson(1.5, (n, k))
    o.sh_pts[:, sk] = rng.poisson(0.05, (n, k))
    o.so_goals[:, sk] = rng.integers(0, 2, (n, k))
    o.goals[:, is_g] = (rng.random((n, 2)) < 0.02).astype(np.int16)
    o.saves[:, is_g] = rng.integers(10, 45, (n, 2))
    o.ga[:, is_g] = rng.integers(0, 6, (n, 2))
    o.decision[:, is_g] = rng.integers(0, 4, (n, 2))
    o.shutout[:, is_g] = ((o.ga[:, is_g] == 0) & (rng.random((n, 2)) < 0.9)).astype(np.int8)
    o.sog[:, is_g] = o.goals[:, is_g]
    return o


def test_arrays_equal_contract_scoring_on_random_lines():
    o = _random_outcomes(np.random.default_rng(3))
    got = score.base_tenths(o)
    names = {v: k for k, v in oc.DECISIONS.items()}
    for s in range(o.n):
        for c in range(o.p):
            if o.is_goalie[c]:
                line = sc.GoalieLine(decision=names[int(o.decision[s, c])], saves=int(o.saves[s, c]),
                                     goals_against=int(o.ga[s, c]), shutout=bool(o.shutout[s, c]), goals=int(o.goals[s, c]),
                                     assists=int(o.assists[s, c]), sog=int(o.sog[s, c]), blocks=int(o.blocks[s, c]),
                                     sh_points=int(o.sh_pts[s, c]))
                want = sc.score_goalie_tenths(line)
            else:
                line = sc.SkaterLine(int(o.goals[s, c]), int(o.assists[s, c]), int(o.sog[s, c]), int(o.blocks[s, c]),
                                     int(o.sh_pts[s, c]), int(o.so_goals[s, c]))
                want = sc.score_skater_tenths(line)
            assert got[s, c] == want, (s, c)


def test_shootout_goals_do_not_feed_goal_bonuses():
    o = oc.empty(["a"], np.array([False]), 1)
    o.so_goals[:] = 3  # three shootout tallies: 3 x 1.5, no hat trick, no 3+ points
    assert score.base_tenths(o)[0, 0] == 3 * sc.SHOOTOUT_GOAL_TENTHS


def test_role_columns_copy_the_person_and_captain_applies_once():
    o = _random_outcomes(np.random.default_rng(4), n=50, p=4)
    base = score.base_tenths(o)
    # Showdown-style role table: person i has a FLEX row (col i) and a CPT row (col 4 + i)
    role_map = np.array([0, 1, 2, 3, 0, 1, 2, 3])
    rb = score.role_tenths(base, role_map)
    assert np.array_equal(rb[:, 0], rb[:, 4]) and np.array_equal(rb[:, 3], rb[:, 7])
    lineups = np.array([[4, 1, 2, 3], [0, 1, 2, 3]])  # CPT-first lineup, then a flex-only lineup
    with_cpt = score.lineup_twentieths(rb, lineups, captain_col=0)
    none = score.lineup_twentieths(rb, lineups, captain_col=None)
    # the captain row's base counts 3 twentieths per tenth instead of 2: the lineup gains exactly 1x base
    assert np.array_equal(with_cpt[:, 0] - none[:, 0], rb[:, 4])
    assert np.array_equal(with_cpt[:, 1] - none[:, 1], rb[:, 0])  # the same slot position in every lineup, once
    only_flex = score.lineup_twentieths(rb, lineups[:, 1:], captain_col=None)
    assert np.array_equal(none[:, 1] - only_flex[:, 1], 2 * rb[:, 0])  # the slot outside the captain column is plain 2x
    for s in (0, 7, 33):
        assert with_cpt[s, 0] == sc.lineup_twentieths([int(x) for x in rb[s, [4, 1, 2, 3]]], 0)
