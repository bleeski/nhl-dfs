import copy

import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.learn import gates
from nhl_dfs.learn.gates import EvidenceCounts, allows, load_floors, tier, validate_floors

pytestmark = pytest.mark.c3


def ownership_counts(mode: Mode, scale: float) -> EvidenceCounts:
    """Counts at `scale` times the ownership floor for the mode (1.0 = exactly on the floor)."""
    groups, labels, hold = (30, 15000, 10) if mode is Mode.CLASSIC else (50, 5000, 15)
    return EvidenceCounts(slate_groups=int(groups * scale), ownership_labels=int(labels * scale),
                          holdout_groups=hold)


def test_floors_config_matches_plan_section_12():
    cfg = load_floors()
    c, s = cfg["tiers"]["classic"], cfg["tiers"]["showdown"]
    assert c["ownership_fit"] == {"slate_groups": 30, "ownership_labels": 15000, "holdout_groups": 10}
    assert s["ownership_fit"] == {"slate_groups": 50, "ownership_labels": 5000, "holdout_groups": 15}
    assert c["rate_correction"] == {"slate_dates": 20, "skater_games": 5000, "goalie_starts": 300}
    assert c["strategy_change"]["slate_groups"] == 100 and c["strategy_change"]["groups_per_family"] == 30
    assert cfg["actions"]["prefit"] == cfg["actions"]["field_fit"] == "ownership_fit"
    assert cfg["actions"]["mixture_fit"] == "strategy_change"


@pytest.mark.parametrize("mode", list(Mode))
def test_prefit_below_floor_refused_above_allowed(mode):
    ok, why = allows("prefit", ownership_counts(mode, 0.9), mode)
    assert not ok and "gated" in why and mode.value in why
    ok, why = allows("prefit", ownership_counts(mode, 1.0), mode)
    assert ok, why


def test_floors_are_per_mode():
    # 40 groups with 16,000 labels clears Classic (30 / 15,000) but not Showdown (50 games).
    counts = EvidenceCounts(slate_groups=40, ownership_labels=16000, holdout_groups=15)
    assert allows("prefit", counts, Mode.CLASSIC)[0]
    ok, why = allows("prefit", counts, Mode.SHOWDOWN)
    assert not ok and "slate_groups 40 < 50" in why


def test_many_labels_from_few_slates_do_not_qualify():
    counts = EvidenceCounts(slate_groups=3, ownership_labels=500_000, holdout_groups=10)
    ok, why = allows("field_fit", counts, Mode.CLASSIC)
    assert not ok and "slate_groups 3 < 30" in why


def test_holdout_must_be_reserved_and_leave_training_groups():
    c = EvidenceCounts(slate_groups=30, ownership_labels=15000, holdout_groups=0)
    ok, why = allows("prefit", c, Mode.CLASSIC)
    assert not ok and "holdout_groups 0 < 10" in why


def test_mixture_fit_needs_family_groups():
    base = dict(slate_groups=120, prospective_groups=30, ownership_labels=99999, holdout_groups=10)
    c = EvidenceCounts(groups_by_family={"large_gpp": 40, "cash": 12}, **base)
    assert allows("mixture_fit", c, Mode.CLASSIC, family="large_gpp")[0]
    ok, why = allows("mixture_fit", c, Mode.CLASSIC, family="cash")
    assert not ok and "groups_per_family 12 < 30" in why
    assert not allows("mixture_fit", c, Mode.CLASSIC)[0]  # no family named: every family must qualify
    assert not allows("mixture_fit", EvidenceCounts(**base), Mode.CLASSIC)[0]


def test_tier_report_orders_tiers():
    empty = tier(EvidenceCounts(), Mode.CLASSIC)
    assert empty.tier == gates.BASE_TIER and not any(empty.allowed.values())
    c = EvidenceCounts(slate_dates=25, skater_games=6000, goalie_starts=400)
    r = tier(c, Mode.CLASSIC)
    assert r.tier == "rate_correction" and r.allowed["rate_correction"] and not r.allowed["prefit"]
    assert r.shortfalls["ownership_fit"]


def test_unknown_action_and_bad_config_raise():
    with pytest.raises(ValueError):
        allows("tune_everything", EvidenceCounts(), Mode.CLASSIC)  # type: ignore[arg-type]
    cfg = copy.deepcopy(load_floors())
    cfg["tiers"]["classic"]["ownership_fit"]["slate_groups"] = -1
    with pytest.raises(ValueError):
        validate_floors(cfg)
    cfg = copy.deepcopy(load_floors())
    del cfg["actions"]["prefit"]
    with pytest.raises(ValueError):
        validate_floors(cfg)
