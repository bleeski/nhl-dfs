"""Backlog B8 (fixed in C8): a PRIOR skater simulates at his ParamTable salary/APPG prior, not the
position-prior rates."""

import copy

import numpy as np
import pytest

from nhl_dfs.models import params as params_mod
from nhl_dfs.sim import game, score
from sim_helpers import slate_for, synthetic_params

pytestmark = pytest.mark.c8


def _base_per_game(p) -> float:
    one = copy.copy(p.opportunity)
    one.p_dress = 1.0
    return params_mod.skater_moments(one, p.rates)[0]


def test_prior_skater_simulates_at_his_param_table_mean():
    t = synthetic_params()
    keys = sorted(t.persons)
    targets = {}
    fwd = [k for k in keys if t.persons[k].group == "F"]
    dmen = [k for k in keys if t.persons[k].group == "D"]
    for k, factor in ((fwd[0], 0.5), (fwd[4], 1.8), (dmen[1], 1.5)):  # depressed and inflated priors
        p = t.persons[k]
        p.prior_mean_tenths = factor * _base_per_game(p)
        targets[k] = p.prior_mean_tenths * p.opportunity.p_dress
    o = game.simulate(slate_for(t), t, 8000, 5)
    mean = score.base_tenths(o).mean(axis=0)
    for k, target in targets.items():
        ratio = mean[keys.index(k)] / target
        assert 0.9 <= ratio <= 1.1, (k, ratio)


def test_history_persons_and_unset_priors_keep_their_rates():
    t = synthetic_params()
    p = next(iter(p for p in t.persons.values() if p.group == "F"))
    assert game.prior_scaled_rates(p) is p.rates  # prior_mean_tenths unset: unchanged
    p.prior_mean_tenths = 40.0
    from nhl_dfs.contracts.statuses import ModelStatus
    p.source = ModelStatus.HISTORY
    assert game.prior_scaled_rates(p) is p.rates
    p.source = ModelStatus.PRIOR
    scaled = game.prior_scaled_rates(p)
    assert scaled is not p.rates and np.isclose(
        scaled.g60["ev"] / p.rates.g60["ev"], scaled.blk60["ev"] / p.rates.blk60["ev"])
