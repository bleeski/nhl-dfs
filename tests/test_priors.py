import copy

import pytest

from conftest import mini_pair, real_pair
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models.priors import (
    PRIORS_YAML,
    load_priors_config,
    prior_objective,
    prior_table,
    validate_priors_config,
)
from pool_builder import make_pool, row, sd_person

pytestmark = pytest.mark.c2a


@pytest.fixture
def cfg():
    return load_priors_config()


def test_config_header_labels_placeholders():
    first = PRIORS_YAML.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# engineering placeholders; replace from history in C5"
    assert "challenger setting" in PRIORS_YAML.read_text(encoding="utf-8")


def test_default_weight_is_point_six(cfg):
    assert cfg["appg_weight"] == 0.6


def test_value_row_is_shrunk_toward_bucket(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "C", 4000, appg=10.0)])
    p = prior_table(pool, cfg)["1"]
    # classic F band <=4999 has mean 72 tenths: 0.6 * 100 + 0.4 * 72 = 88.8
    assert (p.mean_tenths, p.sd_tenths, p.source, p.appg_flag) == (89, 55, "APPG_SHRUNK", "VALUE")
    assert isinstance(p.mean_tenths, int)


def test_appg_zero_gets_zero_weight_and_keeps_flag(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "D", 3000, flag="APPG_ZERO")])
    p = prior_table(pool, cfg)["1"]
    assert (p.mean_tenths, p.source, p.appg_flag) == (44, "BUCKET", "APPG_ZERO")


def test_missing_appg_uses_bucket(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "G", 9000, flag="MISSING")])
    p = prior_table(pool, cfg)["1"]
    assert (p.mean_tenths, p.sd_tenths, p.source, p.appg_flag) == (110, 95, "BUCKET", "MISSING")


def test_band_edges_are_inclusive_and_top_band_is_open(cfg):
    pool = make_pool(Mode.CLASSIC, [
        row(1, "AAA", "C", 3499, flag="MISSING"),
        row(2, "AAA", "C", 3500, flag="MISSING"),
        row(3, "AAA", "C", 99_000, flag="MISSING"),
    ])
    t = prior_table(pool, cfg)
    assert [t[k].mean_tenths for k in "123"] == [40, 72, 133]


def test_weight_is_configurable(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "C", 4000, appg=10.0)])
    c0 = copy.deepcopy(cfg); c0["appg_weight"] = 0.0
    c1 = copy.deepcopy(cfg); c1["appg_weight"] = 1.0
    assert prior_table(pool, c0)["1"].mean_tenths == 72
    assert prior_table(pool, c0)["1"].source == "BUCKET"
    assert prior_table(pool, c1)["1"].mean_tenths == 100


def test_showdown_cpt_uses_flex_band_and_same_prior(cfg):
    # FLEX 5000 is in the showdown F <=5999 band; the CPT salary 7500 would land in the next band.
    cpt, flex = sd_person(1, "AAA", "C", 5000, appg=8.0)
    pool = make_pool(Mode.SHOWDOWN, [cpt, flex])
    t = prior_table(pool, cfg)
    assert t[cpt.role_id] == t[flex.role_id]
    assert t[flex.role_id].mean_tenths == round(0.6 * 80 + 0.4 * 70)


def test_unpaired_cpt_band_uses_salary_over_one_point_five(cfg):
    cpt, _ = sd_person(1, "AAA", "C", 5000, flag="MISSING")
    pool = make_pool(Mode.SHOWDOWN, [cpt])
    assert prior_table(pool, cfg)[cpt.role_id].mean_tenths == 70


def test_objective_applies_captain_multiplier_exactly_once(cfg):
    cpt, flex = sd_person(1, "AAA", "C", 5000, appg=8.0)
    pool = make_pool(Mode.SHOWDOWN, [cpt, flex])
    t = prior_table(pool, cfg)
    obj = prior_objective(pool, t)
    base = t[flex.role_id].mean_tenths / 10.0
    assert obj[flex.role_id] == pytest.approx(base)
    assert obj[cpt.role_id] == pytest.approx(1.5 * base)


def test_classic_objective_has_no_multiplier(cfg):
    pool = make_pool(Mode.CLASSIC, [row(1, "AAA", "C", 4000, appg=10.0)])
    t = prior_table(pool, cfg)
    assert prior_objective(pool, t)["1"] == pytest.approx(8.9)


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.__setitem__("appg_weight", 1.5), "appg_weight"),
    (lambda c: c["buckets"].pop("showdown"), "showdown"),
    (lambda c: c["buckets"]["classic"]["F"][-1].__setitem__("max_salary", 99999), "null"),
    (lambda c: c["buckets"]["classic"]["F"].reverse(), "ascending|null"),
    (lambda c: c["buckets"]["classic"]["G"][0].__setitem__("sd_tenths", 0), "sd_tenths"),
])
def test_bad_config_is_rejected(cfg, mutate, message):
    bad = copy.deepcopy(cfg)
    mutate(bad)
    with pytest.raises(ValueError, match=message):
        validate_priors_config(bad)


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_mini_pools_get_a_prior_for_every_row(mode):
    pool = read_salary(mini_pair(mode)[0])
    t = prior_table(pool)
    assert set(t) == set(pool.by_role_id)
    for r in pool.rows:
        assert t[r.role_id].appg_flag == r.appg_flag
        assert t[r.role_id].sd_tenths > 0


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_pools_zero_appg_rows_are_bucket_only(mode):
    pool = read_salary(real_pair(mode, "2026-09-29")[0])
    t = prior_table(pool)
    assert set(t) == set(pool.by_role_id)
    zeros = [r for r in pool.rows if r.appg_flag == "APPG_ZERO"]
    assert zeros, "fixture should contain APPG_ZERO rows"
    assert all(t[r.role_id].source == "BUCKET" for r in zeros)
    assert all(t[r.role_id].source == "APPG_SHRUNK" for r in pool.rows if r.appg_flag == "VALUE")
