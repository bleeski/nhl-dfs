"""C41 ownership band (flags 119 to 122, docs/experiments/ownership_band_2026-10-16.md): the error-band function on the graded
fixture, the scaled field, the anchored refill and the spread report."""

import copy
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build import scenario_pass as sp_mod
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import FieldCalibration
from nhl_dfs.models import contests as contests_mod
from nhl_dfs.models import field as field_mod
from nhl_dfs.models import ownership
from nhl_dfs.models.projection import PriorProjection
from pool_builder import classic_pool, varied_pool
from test_portfolio import contest, disjoint_lineups, one_field, scenarios_for

pytestmark = pytest.mark.c41

OWN = ownership.load_ownership_config()
RISK = ob.load_risk_config()
FAM = contests_mod.load_contest_families()
EXPO = exposure.load_exposure_config()


@dataclass
class E:
    entry_id: str
    contest_id: str

FIXTURE = Path(__file__).parent / "fixtures" / "c41"


def graded_rows() -> list[ownership.GradedRow]:
    contests = json.loads((FIXTURE / "contests.json").read_text(encoding="utf-8"))
    with open(FIXTURE / "graded_ownership.csv", encoding="utf-8", newline="") as fh:
        return [ownership.GradedRow(contests[r["contest_id"]]["forecast_run"], r["contest_id"], contests[r["contest_id"]]["family"],
                                    contests[r["contest_id"]]["mode"], r["role_id"], float(r["pred_pct"]), float(r["actual_pct"]))
                for r in csv.DictReader(fh)]


# -- the band (flag 119) ------------------------------------------------------------------------------------------------

def test_the_band_function_reproduces_the_0930_mae_of_the_settle_grades():
    band = ownership.error_band(graded_rows())
    got = {c.contest_id: c for c in band.contests}
    # runs/20260930-233424-classic/settle/grades.json, mae_all and mae_popular (the synthesis review's "MAE 3.9 to 4.3")
    want = {"196218433": (4.106, 10.107), "196218434": (4.034, 11.147), "196218438": (4.315, 11.279), "196228907": (3.942, 10.034)}
    for cid, (mae, popular) in want.items():
        c = got[cid]
        assert (c.mae, c.mae_popular, c.forecast_run, c.mode) == (mae, popular, "20260930-214909-classic", "classic")
        assert 3.9 <= c.mae <= 4.33


def test_the_other_graded_forecasts_reproduce_their_grades_too():
    got = {c.contest_id: c for c in ownership.error_band(graded_rows()).contests}
    want = {"195958173": 2.233, "196048725": 2.83, "196048727": 3.047, "196050215": 2.594}  # 09-29 Classic and Showdown
    assert {cid: got[cid].mae for cid in want} == want
    assert set(got) == set(want) | {"196218433", "196218434", "196218438", "196228907"}  # large_gpp only: the cash contest is out


def test_every_graded_contest_is_flatter_than_its_forecast_and_the_band_is_the_mirror_of_the_flattest():
    band = ownership.error_band(graded_rows())
    assert all(0.7 < c.slope < 0.86 for c in band.contests)  # planning saw 0.715 to 0.850 (the preregistration's table)
    assert {f: v["mode"] for f, v in band.pooled.items()} == {"20260929-222125-classic": "classic", "20260930-214909-classic": "classic",
                                                              "20260930-013521-showdown": "showdown"}
    classic = {f: v["slope"] for f, v in band.pooled.items() if v["mode"] == "classic"}
    assert band.beta_low["classic"] == min(classic.values()) == classic["20260930-214909-classic"]
    assert band.beta_high["classic"] == pytest.approx(1.0 / band.beta_low["classic"], abs=1e-4)
    assert 0.72 < band.beta_low["classic"] < 0.76


def test_the_pooled_slope_weights_each_contest_by_its_own_spread():
    rows = [ownership.GradedRow("f", "1", "large_gpp", "classic", "a", 10.0, 8.0),
            ownership.GradedRow("f", "1", "large_gpp", "classic", "b", 0.0, 2.0),
            ownership.GradedRow("f", "2", "large_gpp", "classic", "a", 20.0, 20.0),
            ownership.GradedRow("f", "2", "large_gpp", "classic", "b", 0.0, 0.0),
            ownership.GradedRow("f", "3", "cash", "classic", "a", 30.0, 0.0),
            ownership.GradedRow("f", "3", "cash", "classic", "b", 0.0, 30.0)]
    band = ownership.error_band(rows)
    # contest 1: dp (5, -5), da (3, -3): xy 30, xx 50; contest 2: xy 200, xx 200; cash left out of the pool
    assert [c.slope for c in band.contests] == [0.6, 1.0, -1.0]
    assert band.pooled == {"f": {"mode": "classic", "contests": ["1", "2"], "slope": round(230 / 250, 4)}}
    assert band.beta_low == {"classic": 0.92} and band.beta_high == {"classic": round(1 / 0.92, 4)}
    assert ownership.concentration_slope([1.0, 1.0], [0.0, 5.0]) != ownership.concentration_slope([1.0, 1.0], [0.0, 5.0])  # nan


def test_the_fixture_carries_no_names_or_lineups():
    head = (FIXTURE / "graded_ownership.csv").read_text(encoding="utf-8").splitlines()[0]
    assert head == "contest_id,role_id,pred_pct,actual_pct"
    assert all(set(v) == {"forecast_run", "graded_run", "family", "mode"}
               for v in json.loads((FIXTURE / "contests.json").read_text(encoding="utf-8")).values())


# -- the scaled field (flag 120) --------------------------------------------------------------------------------------------

def test_scaled_behaviors_scale_only_the_noise_and_one_is_the_same_list():
    beh = field_mod.behaviors_for("large_gpp", OWN, mode=Mode.CLASSIC, games=3)
    assert field_mod.scaled_behaviors(beh, 1.0) == beh
    hot = field_mod.scaled_behaviors(beh, 2.0)
    assert [b.noise_sd for b in hot] == [2.0 * b.noise_sd for b in beh]
    assert [(b.name, b.weight, b.stack_rule, b.popularity_weight) for b in hot] == [
        (b.name, b.weight, b.stack_rule, b.popularity_weight) for b in beh]
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            field_mod.scaled_behaviors(beh, bad)


def _field(t: float, seed: int = 11):
    pool = varied_pool(Mode.CLASSIC, seed=0)
    proj = PriorProjection(pool)
    feats = ownership.feature_table(pool, proj, cfg=OWN)
    util = ownership.perceived(pool, proj, feats, ownership.family_weights(OWN, "large_gpp"))
    beh = field_mod.scaled_behaviors(field_mod.behaviors_for("large_gpp", OWN), t)
    return pool, field_mod.sample(pool, Mode.CLASSIC, util, beh, 120, seed, "large_gpp", proj=proj, feats=feats, cfg=OWN)


def test_temperature_one_is_the_base_field_and_a_hotter_field_is_flatter():
    pool, base = _field(1.0)
    _, same = _field(1.0)
    _, hot = _field(4.0)
    assert same.keys == base.keys and same.behavior_id == base.behavior_id
    b, h = field_mod.marginals(base, pool, 1).own, field_mod.marginals(hot, pool, 1).own
    roles = sorted(b)
    top = max(roles, key=lambda r: (b[r], r))
    assert h[top] < b[top]
    assert ownership.concentration_slope([b[r] for r in roles], [h[r] for r in roles]) < 1.0
    assert len(set(hot.keys)) > len(set(base.keys))  # flatter: fewer repeated lineups


# -- the shared per-candidate utility -----------------------------------------------------------------------------------

def _ge(rng, S, K, L):
    return rng.integers(0, L, (S, K)), rng.integers(0, 3, (S, K))


def test_field_utility_sums_exactly_what_the_screen_summed_before():
    rng = np.random.default_rng(41)
    ct = contest("C", "large_gpp", 500, [5000, 2000, 1000, 500, 300])
    G, E1 = _ge(rng, 3000, 37, 520)
    cv = ob.PayCurve(ct, float(RISK["objectives"]["top_pct"]), ob.goal_of(RISK))
    su = np.zeros(37)
    for a in range(0, 3000, 701):  # the screen's old loop; any chunking gives the same integer sums
        _, util = cv.pay_util(G[a:a + 701], E1[a:a + 701])
        su += util.sum(axis=0)
    fu = ob.field_utility(ct, G, E1, RISK)
    assert np.array_equal(fu.total, su)
    assert fu.diff is None and fu.diff_se is None


def test_field_utility_paired_difference_and_its_se():
    rng = np.random.default_rng(42)
    ct = contest("C", "large_gpp", 300, [3000, 1000, 500])
    G, E1 = _ge(rng, 2000, 9, 310)
    G2, E2 = _ge(rng, 2000, 9, 310)
    fu = ob.field_utility(ct, G, E1, RISK, other=(G2, E2))
    cv = ob.PayCurve(ct, float(RISK["objectives"]["top_pct"]), ob.goal_of(RISK))
    u1, u2 = cv.pay_util(G, E1)[1].astype(float), cv.pay_util(G2, E2)[1].astype(float)
    d = u2 - u1
    assert np.allclose(fu.mean, u1.mean(axis=0)) and np.allclose(fu.se, u1.std(axis=0) / np.sqrt(2000))
    assert np.allclose(fu.diff, d.mean(axis=0)) and np.allclose(fu.diff_se, d.std(axis=0) / np.sqrt(2000))
    same = ob.field_utility(ct, G, E1, RISK, other=(G, E1))
    assert not same.diff.any() and not same.diff_se.any()


# -- the anchored refill and the flip it names (flag 121) -------------------------------------------------------------------

def _flip_case():
    """One large_gpp contest of 2 with one opponent. Against the base field (lineup f) candidate a beats f every time and b in
    98% of draws, so a is the band's anchor. Against the alternative field (lineup b) b ties itself every time (half of first)
    while a beats b only 40% of the time, so b is the anchor there: the pick and the anchor both move."""
    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    S = 2000
    rng = np.random.default_rng(7)
    fld = rng.integers(300, 700, S)
    r = rng.random(S)
    tot_b = np.where(r < 0.6, fld + 5, np.where(r < 0.98, fld + 1, fld - 1))
    scen = scenarios_for(pool, [a, b, f], [fld + 2, tot_b, fld])
    caps = exposure.caps(EXPO, 1, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    ct = {"C": contest("C", "large_gpp", 2, [180])}
    own = {a.role_ids[0]: 5.0, b.role_ids[0]: 60.0}  # a is also the lower-owned, so the base pick is the anchor
    sel = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, pf.RiskBudget(1.0, None, None, None),
                    seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own}, keep_prepared=True)
    return pool, a, b, f, scen, caps, ct, own, sel


def test_without_keep_prepared_select_keeps_nothing_and_picks_the_same():
    pool, a, b, f, scen, caps, ct, own, sel = _flip_case()
    plain = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, pf.RiskBudget(1.0, None, None, None),
                      seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own})
    assert plain.prepared is None and sel.prepared is not None
    assert plain.by_entry == sel.by_entry and plain.chosen_kappa == sel.chosen_kappa
    assert plain.choices["e1"] == sel.choices["e1"]


def test_the_base_field_as_the_alternative_changes_nothing():
    pool, a, b, f, scen, caps, ct, own, sel = _flip_case()
    rep = pf.band_report(sel, {"C": one_field(f)}, {"C": own}, {}, scen, ct, caps, pool=pool, fam_cfg=FAM, risk_cfg=RISK,
                         field_cal=FieldCalibration.PRIOR, free=True)
    x = rep["entries"]["e1"]
    assert (x["pick_flip"], x["anchor_flip"], x["free_flip"]) == (False, False, False)
    assert x["alt_key"] == x["base_key"] == a.key and rep["added_by_alt_screen"] == 0
    pc = rep["per_contest"]["C"]
    assert not pc["d"].any() and not pc["d_se"].any()
    assert pc["base"][rep["keys"].index(a.key)] == pytest.approx(1.8)


def test_an_alternative_field_that_moves_the_anchor_is_named_with_the_pick():
    pool, a, b, f, scen, caps, ct, own, sel = _flip_case()
    assert sel.choices["e1"].key == a.key and sel.choices["e1"].anchor_key == a.key
    rep = pf.band_report(sel, {"C": one_field(b)}, {"C": own}, {}, scen, ct, caps, pool=pool, fam_cfg=FAM, risk_cfg=RISK,
                         field_cal=FieldCalibration.PRIOR, free=True)
    x = rep["entries"]["e1"]
    assert (x["alt_key"], x["alt_anchor"], x["pick_flip"], x["anchor_flip"], x["free_flip"]) == (b.key, b.key, True, True, True)
    pc, keys = rep["per_contest"]["C"], rep["keys"]
    ia, ib = keys.index(a.key), keys.index(b.key)
    assert pc["d"][ia] == pytest.approx(0.4 * 1.8 - 1.8, abs=0.06) and pc["d_se"][ia] > 0  # a now wins only 40% of first
    assert pc["d"][ib] == pytest.approx(0.9 - 0.98 * 1.8, abs=0.06)  # b: half of first every time, from first in 98%


def test_the_band_average_with_the_base_field_twice_is_the_one_field_fill():
    pool, a, b, f, scen, caps, ct, own, sel = _flip_case()
    twice = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, pf.RiskBudget(1.0, None, None, None),
                      seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own},
                      band_fields={"low": ({"C": one_field(f)}, {"C": own}, {}), "high": ({"C": one_field(f)}, {"C": own}, {})})
    assert twice.by_entry == sel.by_entry and twice.chosen_kappa == sel.chosen_kappa
    x, y = twice.choices["e1"], sel.choices["e1"]
    assert (x.key, x.anchor_key) == (y.key, y.anchor_key)
    # (u + u + u) / 3 is u only to rounding: the averaged path is its own arithmetic; the switch-off path is the original code
    assert (x.score, x.score_se, x.band) == pytest.approx((y.score, y.score_se, y.band), rel=1e-12, abs=1e-6)
    assert twice.screened["ownership_band"] == {"fields": ["high", "low"], "added_by_band_screens": 0}
    assert [(p.kappa, p.tail_utility) for p in twice.frontier] == [(p.kappa, p.tail_utility) for p in sel.frontier]


def test_the_band_average_picks_the_lineup_that_holds_up_in_both_fields():
    pool, a, b, f, scen, caps, ct, own, sel = _flip_case()
    band = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, pf.RiskBudget(1.0, None, None, None),
                     seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own},
                     band_fields={"high": ({"C": one_field(b)}, {"C": own}, {})})
    # a: 1.8 against f, 0.72 against b (mean 1.26); b: 1.764 and 0.9 (mean 1.332): b holds up and is outside a's band
    assert sel.choices["e1"].key == a.key and band.choices["e1"].key == b.key
    assert band.choices["e1"].score == pytest.approx((0.98 * 1.8 + 0.9) / 2, abs=0.05)


def test_the_band_term_says_whether_the_floor_or_the_se_set_the_width():
    ch = pf.Choice("e", "C", "large_gpp", 0, "k", "central", 1.0, 0.01, 0.03, 0, 0.0, 0.0, "proxy", 0, "k", 0.0, 0.0, 1.0, 0.01)
    assert pf.band_term(ch, RISK) == "floor"  # 0.03 x 1.0 above 1.0 x 0.01
    ch.anchor_se = 0.05
    assert pf.band_term(ch, RISK) == "se"


def test_band_specs_are_classic_only_and_need_both_temperatures():
    cfg = copy.deepcopy(OWN)
    cfg["field"]["error_band"].update(report=True)
    cfg["field"]["error_band"]["classic"].update(t_low=1.5, t_high=0.7)
    assert sp_mod.band_specs(cfg, Mode.CLASSIC) == ([("low", 1.5, 0), ("high", 0.7, 0)], None)
    assert sp_mod.band_specs(cfg, Mode.SHOWDOWN) == (None, "SKIPPED (C41 calibrated Classic only)")
    cfg["field"]["error_band"].update(report=False)
    assert sp_mod.band_specs(cfg, Mode.CLASSIC)[0] is None
    cfg["field"]["error_band"].update(report=True)
    cfg["field"]["error_band"]["classic"].update(t_low=None)
    with pytest.raises(ValueError):
        ownership.validate_ownership_config(cfg)


# -- the report in a run (flag 122) -------------------------------------------------------------------------------------------

def _own_cfg(report: bool, t_low: float | None, t_high: float | None) -> dict:
    cfg = copy.deepcopy(OWN)
    cfg["field"]["error_band"].update(report=report)
    cfg["field"]["error_band"]["classic"].update(t_low=t_low, t_high=t_high)
    ownership.validate_ownership_config(cfg)
    return cfg


@pytest.fixture(scope="module")
def mini_runs(tmp_path_factory):
    """The mini Classic slate through the whole scenario pass with the report off, on (t 2.0 and 0.5) and on at t = 1."""
    from conftest import mini_pair
    from nhl_dfs.build.run import run_slate

    small = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}
    real, real_risk = ownership.load_ownership_config, ob.load_risk_config
    switch = copy.deepcopy(RISK)
    switch["selection"]["ownership_band"] = True
    out = {}
    try:
        for name, cfg in (("off", _own_cfg(False, None, None)), ("on", _own_cfg(True, 2.0, 0.5)), ("one", _own_cfg(True, 1.0, 1.0)),
                          ("switch", _own_cfg(True, 2.0, 0.5))):
            ownership.load_ownership_config = lambda path=ownership.OWNERSHIP_YAML, cfg=cfg: copy.deepcopy(cfg)
            ob.load_risk_config = (lambda path=ob.RISK_YAML: copy.deepcopy(switch)) if name == "switch" else real_risk
            tmp = tmp_path_factory.mktemp(f"c41_{name}")
            r = run_slate(*mini_pair("classic"), offline=True, baseline_only=False, scenario=True, scenario_n=small,
                          out_root=tmp / "runs", outputs_root=tmp / "outputs",
                          clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
            assert r.ok, r.manifest["failed"]
            files = sorted((tmp / "outputs").rglob("DKEntries.csv"))
            out[name] = (r, files[-1].read_bytes() if files else None)
    finally:
        ownership.load_ownership_config, ob.load_risk_config = real, real_risk
    return out


@pytest.mark.timeout(900)  # four whole offline scenario passes in the module fixture
def test_the_switch_off_by_default_and_on_it_scores_on_the_band(mini_runs):
    assert RISK["selection"]["ownership_band"] is False
    off = mini_runs["off"][0].manifest["scenario"]
    assert "selection_ownership_band" not in off and "ownership_band" not in off["discovery"]["screened"]
    r, data = mini_runs["switch"]
    sec = r.manifest["scenario"]
    assert data is not None and sec["selection_ownership_band"]["on"] is True
    assert sec["selection_ownership_band"]["t_low"] == 2.0 and sec["timings"]["ownership_band_fields_s"] > 0
    assert sec["discovery"]["screened"]["ownership_band"]["fields"] == ["high", "low"]
    assert sec["ownership_bands"]["skipped"].startswith("SKIPPED (the selection already scores on the band")


@pytest.mark.timeout(900)  # four whole offline scenario passes in the module fixture
def test_the_report_never_changes_the_published_file(mini_runs):
    off, on, one = (mini_runs[k][1] for k in ("off", "on", "one"))
    assert off is not None and off == on == one
    assert mini_runs["off"][0].manifest["scenario"]["ownership_bands"]["skipped"].startswith("report off")


@pytest.mark.timeout(900)  # four whole offline scenario passes in the module fixture
def test_the_report_prints_the_spread_and_names_the_picks(mini_runs):
    r = mini_runs["on"][0]
    b = r.manifest["scenario"]["ownership_bands"]
    assert "error" not in b and "skipped" not in b, b
    low, high = b["fields"]["low"]["families"]["large_gpp"], b["fields"]["high"]["families"]["large_gpp"]
    assert low["slope_vs_base"] < 1.0 < high["slope_vs_base"] and low["top_alt"] < low["top_base"] < high["top_alt"]
    assert b["spread"] and all(v["candidates"] > 0 for v in b["spread"].values())
    assert set(b["picks"]) == {"low", "high"} and b["pick_spread"]
    assert b["file"] == "scenario/ownership_bands.json"
    assert all(n.endswith(")") for f in b["flips"] for n in f["out"] + f["in"])  # name (team)
    saved = json.loads((r.run.path / b["file"]).read_text(encoding="utf-8"))
    assert set(saved["by_field"]) == {"low", "high"}
    for cid, v in b["spread"].items():
        assert len(saved["spread"][cid]["spread"]) == len(saved["keys"]) == v["candidates"]
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    assert "C41 ownership band, report only" in notes and "Tail spread, contest" in notes and "Picks at knob" in notes
    for f in b["flips"]:
        assert f"{f['entry_id']} ({f['contest_id']}, {f['field']}" in notes


@pytest.mark.timeout(900)  # four whole offline scenario passes in the module fixture
def test_temperature_one_in_a_run_rebuilds_the_base_field_exactly(mini_runs):
    b = mini_runs["one"][0].manifest["scenario"]["ownership_bands"]
    for label in ("low", "high"):
        fam = b["fields"][label]["families"]["large_gpp"]
        assert (fam["slope_vs_base"], fam["mae_vs_base"], fam["top_alt"]) == (1.0, 0.0, fam["top_base"])
        assert b["picks"][label]["pick_flips"] == b["picks"][label]["anchor_flips"] == 0
    assert b["flips"] == [] and all(v["max_spread"] == 0.0 for v in b["spread"].values())


def test_run_notes_name_every_moved_pick_by_name_and_team():
    from nhl_dfs.build import notes

    b = {"label": "C41 ownership band, report only", "band": {"beta_low": 0.7369, "beta_high": 1.357}, "seconds": 12.0,
         "fields": {"low": {"temperature": 1.6818, "families": {"large_gpp": {"slope_vs_base": 0.75, "mae_vs_base": 1.3,
                                                                             "top_base": 64.8, "top_alt": 47.2}}}},
         "spread": {"C": {"family": "large_gpp", "goal": "first_place_equity", "utility": "first-place dollars", "candidates": 3,
                          "median_spread": 0.1, "max_spread": 0.5, "median_z": 2.1, "share_z_over_2": 0.5}},
         "picks": {"low": {"entries": 2, "pick_flips": 1, "anchor_flips": 1, "large_gpp_picks": 2, "large_gpp_pick_flips": 1,
                           "large_gpp_anchor_flips": 1, "added_by_alt_screen": 0}},
         "flips": [{"entry_id": "7000000001", "contest_id": "C", "field": "low", "anchor_moved": True,
                    "out": ["Sebastian Aho (CAR)"], "in": ["Sebastian Aho (NYI)"]}],
         "chosen_kappa": 0.25, "file": "scenario/ownership_bands.json", "notes": []}
    text = "\n".join(notes._band_lines(b))
    assert "7000000001 (C, low, anchor moved): out Sebastian Aho (CAR); in Sebastian Aho (NYI)" in text
    assert "first-place dollars" in text and "high not built" in text and "Picks at knob 0.25 under low: 1 of 2" in text
