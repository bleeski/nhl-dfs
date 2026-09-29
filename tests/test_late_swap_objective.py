"""C9: objective-aware late swap and refresh (scenario, then provisional, then baseline)."""

import copy

import numpy as np
import pytest

from nhl_dfs.sim import game, score
from sim_helpers import slate_for, synthetic_params

pytestmark = pytest.mark.c9

TEAMS = ("AAA", "BBB", "CCC", "DDD")
GAMES = (("AAA", "BBB"), ("CCC", "DDD"))


def _base(slate, params, n, games=None, seed=11, purpose="selection"):
    return np.concatenate([score.base_tenths(o) for o in game.iter_chunks(slate, params, n, seed, purpose, games)])


def _cols(params, teams):
    keys = sorted(params.persons)
    return [i for i, k in enumerate(keys) if params.persons[k].team in teams]


# -- per-game independence: the premise of re-simulating only changed games --------------------------

def test_a_filtered_game_draws_exactly_what_the_full_slate_draws():
    params = synthetic_params(TEAMS)
    slate = slate_for(params, GAMES)
    full = _base(slate, params, 2500)  # two chunks
    only = _base(slate, params, 2500, games={"DDD@CCC"})
    c = _cols(params, {"CCC", "DDD"})
    other = _cols(params, {"AAA", "BBB"})
    assert np.array_equal(full[:, c], only[:, c])
    assert not only[:, other].any()


def test_changing_one_team_leaves_the_other_games_columns_identical():
    params = synthetic_params(TEAMS)
    changed = copy.deepcopy(params)
    k = next(k for k, p in sorted(changed.persons.items()) if p.team == "AAA" and p.group == "F")
    changed.persons[k].opportunity.toi_ev_s *= 1.4
    a = _base(slate_for(params, GAMES), params, 2000)
    b = _base(slate_for(changed, GAMES), changed, 2000)
    c = _cols(params, {"CCC", "DDD"})
    assert np.array_equal(a[:, c], b[:, c])
    assert not np.array_equal(a[:, _cols(params, {"AAA", "BBB"})], b[:, _cols(params, {"AAA", "BBB"})])
