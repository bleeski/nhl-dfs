"""C11: grades on synthetic forecasts reproduce known errors; gate reports; backlog appends; run notes."""
import math

import numpy as np
import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.learn import grade_ownership as go
from nhl_dfs.learn.standings import Joined
from nhl_dfs.models.field import Marginals
from pool_builder import make_pool, row, sd_person

pytestmark = pytest.mark.c11


def marg(own, dup=None, field_size=100):
    return Marginals("large_gpp", field_size - 1, field_size, own, {}, {}, dup or {}, {}, {}, 0, False)


def joined(own, mode=Mode.CLASSIC, n=100, dup=None, complete=True, lineups=None):
    return Joined(1, mode, n, own, {}, lineups if lineups is not None else {str(i): ["x"] for i in range(n)},
                  dup or {}, complete)


def test_ownership_grade_reproduces_known_errors():
    pool = make_pool(Mode.CLASSIC, [row(i, "AAA" if i <= 2 else "BBB", "C", 4000) for i in range(1, 5)])
    f = marg({"1": 50.0, "2": 10.0, "3": 0.0, "4": 4.0}, dup={"k1": 10.0}, field_size=200)
    a = joined({"1": 40.0, "2": 10.0, "3": 6.0}, dup={"k1": 3, "k2": 7})
    g = go.grade(f, a, pool=pool)
    assert g.mae_all == pytest.approx((10 + 0 + 6 + 4) / 4)
    assert g.mae_active == pytest.approx((10 + 0 + 6 + 4) / 4)  # every role is active (actual > 0 or pred >= 0.5)
    assert g.weighted_mae == pytest.approx((40 * 10 + 10 * 0 + 6 * 6) / 56, abs=1e-3)  # rounded to 3 places
    assert g.zero_observed_mass == pytest.approx(4.0)  # the forecast's mass on the role nobody drafted
    assert g.team_total_mae == pytest.approx((abs(60 - 50) + abs(4 - 6)) / 2)
    band = {b["band"]: b for b in g.band_calibration}
    assert band["40-101%"] == {"band": "40-101%", "n": 1, "mean_pred": 50.0, "mean_actual": 40.0}
    assert g.dup_count_err["scale"] == pytest.approx(0.5)  # 100 actual entries, forecast at 200
    assert g.dup_count_err["top_predicted"][0] == {"lineup": "k1", "pred_scaled": 5.0, "actual": 3}
    assert g.dup_count_err["top_actual"][0]["lineup"] == "k2" and g.dup_count_err["top_actual"][0]["pred_scaled"] == 0
    assert g.pearson == pytest.approx(float(np.corrcoef([50, 10, 0, 4], [40, 10, 6, 0])[0, 1]), abs=1e-3)


def test_without_observed_zeros_only_drafted_roles_are_graded_and_captain_share_is_separate():
    rows = [r for pid in range(1, 4) for r in sd_person(pid, "AAA", "C", 5000)]
    pool = make_pool(Mode.SHOWDOWN, rows)
    f = marg({"1c": 20.0, "1f": 60.0, "2c": 30.0, "2f": 40.0, "3c": 50.0, "3f": 10.0})
    a = joined({"1c": 10.0, "1f": 70.0, "2c": 40.0}, mode=Mode.SHOWDOWN, complete=False)
    g = go.grade(f, a, pool=pool)
    assert g.n_roles == 3 and g.zero_observed_mass is None and not g.complete
    assert g.cpt_share_err == pytest.approx((10 + 10) / 2)  # only the graded CPT roles 1c and 2c
    assert any("not observed" in n for n in g.notes)
    assert math.isnan(g.pearson) or -1 <= g.pearson <= 1
