"""C40: the measurement script's bound and mechanical verdict.

These tests protect the second look: the rule's thresholds live in `scripts/c40_measure.py` and must match
`docs/experiments/2026-10-07_c40_df_stamp_rule.md`. They are NOT the card's exit check (CHI keeps its lines labeled stale), which tests
behavior that was not built because the first look was BLOCKED (docs/sources.md, C40 section). No network, no raw data.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.c40

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("c40_measure", REPO / "scripts" / "c40_measure.py")
m = importlib.util.module_from_spec(_spec)
sys.modules["c40_measure"] = m
_spec.loader.exec_module(m)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def fetch(minutes: int, stamp_h: float = 0.0, f1=(1, 2, 3), pp1=(1, 2), team="CHI") -> "m.Fetch":
    return m.Fetch(team, T0 + timedelta(minutes=minutes), f"h{minutes}", T0 + timedelta(hours=stamp_h),
                   {"f1": frozenset(f1), "pp1": frozenset(pp1)}, {}, (), frozenset(), None, [])


def cells4(a, b, c, d):
    return {"A": a, "B": b, "C": c, "D": d}


# -- the bound ---------------------------------------------------------------------------------------------------------

def test_wilson_upper_at_the_first_look_boundary():
    assert m.wilson_upper(0, 0) is None
    assert 0.1005 < m.wilson_upper(4, 85) < 0.1015  # 4 misses in 85 real changes: 10.08%, over the 10% limit
    assert m.wilson_upper(3, 85) < 0.10  # one miss fewer would have passed (8.5%)
    assert m.wilson_upper(5, 85) > 0.11


# -- cells, backward stamps, thinning -------------------------------------------------------------------------------------

def test_cells_put_a_missed_change_in_c():
    a, b = fetch(0), fetch(60, f1=(1, 2, 9))  # lines changed, stamp not moved: the dangerous cell
    c, d = fetch(120, stamp_h=1.0, f1=(1, 2, 9)), fetch(180, stamp_h=1.0, f1=(1, 2, 9))
    assert m.cells([(a, b)], m.SIGS["EV"]) == cells4(0, 0, 1, 0)
    assert m.cells([(b, c)], m.SIGS["EV"]) == cells4(0, 0, 0, 0) | {"B": 1}  # stamp moved, lines same
    assert m.cells([(c, d)], m.SIGS["EV"]) == cells4(0, 0, 0, 1)
    e = fetch(240, stamp_h=2.0, f1=(4, 5, 6))
    assert m.cells([(d, e)], m.SIGS["EV"]) == cells4(1, 0, 0, 0)
    assert m.cells([(a, b)], m.SIGS["PP"]) == cells4(0, 0, 0, 1)  # the PP signature did not change


def test_a_stamp_going_backward_is_removed_before_anything_is_counted():
    fs = [fetch(0, stamp_h=5.0), fetch(60, stamp_h=1.0), fetch(120, stamp_h=6.0)]  # the middle one is a stale cached copy
    cl, removed = m.clean({"CHI": fs})
    assert [f.hash for f in cl["CHI"]] == ["h0", "h120"] and [f.hash for f in removed] == ["h60"] and removed[0].regressed


def test_thinning_keeps_a_fetch_only_20_minutes_after_the_last_kept_one():
    fs = [fetch(0), fetch(10), fetch(20), fetch(35), fetch(40)]
    assert [f.hash for f in m.thin(fs)] == ["h0", "h20", "h40"]


# -- the verdict, mechanical -----------------------------------------------------------------------------------------------

def passing_r():
    return {"12-24 h": {"n": 66, "ev": 35, "pp": 35, "keys": []}, "24-48 h": {"n": 45, "ev": 30, "pp": 30, "keys": []},
            "48-72 h": {"n": 20, "ev": 12, "pp": 12, "keys": []}, "72-96 h": {"n": 1, "ev": 1, "pp": 1, "keys": []}}


def test_the_first_look_is_blocked_not_adopted():
    allc = {"EV": cells4(81, 117, 4, 921), "PP": cells4(47, 151, 2, 923)}
    r = {"12-24 h": {"n": 66, "ev": 35, "pp": 48, "keys": []}, "24-48 h": {"n": 45, "ev": 28, "pp": 40, "keys": []},
         "48-72 h": {"n": 6, "ev": 4, "pp": 4, "keys": []}, "72-96 h": {"n": 1, "ev": 1, "pp": 1, "keys": []}}
    v, trace = m.verdict(allc, r, 49)
    assert v.startswith("BLOCKED") and "upper bound" in v
    assert any("U(EV) = 10.1%" in t for t in trace)


def test_above_five_percent_is_a_reject_and_below_the_floors_is_blocked():
    v, _ = m.verdict({"EV": cells4(79, 100, 6, 900), "PP": cells4(40, 100, 0, 900)}, passing_r(), 60)
    assert v.startswith("REJECT")
    v, _ = m.verdict({"EV": cells4(20, 100, 0, 900), "PP": cells4(40, 100, 0, 900)}, passing_r(), 60)
    assert v.startswith("BLOCKED") and "floor" in v  # 20 real changes is under the floor of 30
    v, _ = m.verdict({"EV": cells4(80, 100, 1, 900), "PP": cells4(40, 100, 0, 900)}, passing_r(), 20)
    assert v.startswith("BLOCKED") and "floor" in v  # 20 stale episodes is under the floor of 30


def test_a_failing_24_48_band_rejects():
    r = passing_r()
    r["24-48 h"] = {"n": 40, "ev": 12, "pp": 12, "keys": []}  # 30% against a reference of 53%
    v, _ = m.verdict({"EV": cells4(84, 100, 1, 900), "PP": cells4(40, 100, 0, 900)}, r, 60)
    assert v.startswith("REJECT")


def test_a_thin_48_72_band_caps_the_keep_at_48_h_and_a_weak_pp_drops_pp_units():
    r = passing_r()
    r["48-72 h"] = {"n": 6, "ev": 4, "pp": 4, "keys": []}  # COL's band: six episodes, not measured
    v, trace = m.verdict({"EV": cells4(82, 100, 3, 900), "PP": cells4(47, 151, 2, 923)}, r, 60)
    assert v.startswith("ADOPT WITH A LIMIT") and "cap 48 h" in v and "PP units not kept" in v
    assert any("not measured" in t for t in trace)


def test_adopt_needs_both_stale_bands_and_pp():
    v, _ = m.verdict({"EV": cells4(84, 100, 1, 900), "PP": cells4(41, 100, 0, 900)}, passing_r(), 60)
    assert v.startswith("ADOPT (cap 72 h")
