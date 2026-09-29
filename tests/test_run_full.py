"""C8 full run: baseline, provisional and scenario versions, each checked by the referee, with the
objective metrics and the three evidence states in RUN_NOTES. Functional only: the 5-minute budget
is measured on the real files in the exit check, never as a wall-clock assertion here."""

from datetime import datetime, timezone

import pytest

from conftest import mini_pair
from nhl_dfs.build import objectives as ob
from nhl_dfs.build.run import run_slate
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c8

BEFORE = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)  # pinned before the 2026-09-29 slates
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _run(tmp, mode, **kw):
    return run_slate(*mini_pair(mode), offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                     out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: BEFORE, **kw)


@pytest.fixture(scope="module")
def classic(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("full_classic"), "classic")


def test_full_run_publishes_three_checked_versions(classic):
    r = classic
    assert r.ok, r.manifest["failed"]
    assert [v["phase"] for v in r.manifest["versions"]] == ["A", "P", "S"]
    for v in r.manifest["versions"]:
        rep = check_file(v["path"], r.run.inputs / "DKSalaries.csv", r.run.inputs / "DKEntries.csv")
        assert rep.ok, rep.reasons[:3]
        assert rep.out_sha256 == v["sha256"]
    s = r.manifest["scenario"]
    assert s["version"] == 3 and not r.manifest["failed"]
    assert s["evidence"]["OUTCOME_CALIBRATION"] == "UNVALIDATED"
    for k in ("PAYOUT_SOURCE", "OUTCOME_CALIBRATION", "FIELD_CALIBRATION"):
        assert r.statuses[k] == s["evidence"][k]
    assert set(s["game_sources"].values()) == {"MODEL"}  # offline: no market price is ever claimed


def test_scenario_figures_carry_their_errors_and_the_notes_say_so(classic):
    s = classic.manifest["scenario"]
    for e in s["entries"]:
        assert e["se"] >= 0 and "exp_payout_se" in e and "p_top1pct_se" in e
        assert e["objective"] == ob.FAMILY_OBJECTIVE[e["family"]]
    pf = s["portfolio"]
    assert pf["measured_on"] == "referee scenarios" and 0 <= pf["p_lose80"] <= 1 and pf["p_lose80_se"] >= 0
    notes = classic.notes_path.read_text(encoding="utf-8")
    assert "## Scenario portfolio (C8)" in notes
    assert "OUTCOME_CALIBRATION=UNVALIDATED" in notes and "FIELD_CALIBRATION=" in notes and "PAYOUT_SOURCE=" in notes
    assert "+/-" in notes and "uncalibrated scenario proxies" in notes
    assert "MODEL" in notes


def test_frontier_report_has_no_dominated_points(classic):
    pts = classic.manifest["scenario"]["frontier"]
    assert pts
    for p in pts:
        assert not p["dominated"]
        assert not any(q["tail_utility"] >= p["tail_utility"] and q["p_lose80"] <= p["p_lose80"]
                       and (q["tail_utility"] > p["tail_utility"] or q["p_lose80"] < p["p_lose80"]) for q in pts)


def test_infeasible_budget_yields_least_risk_point_and_still_a_file(tmp_path, monkeypatch):
    cfg = ob.load_risk_config()
    for mode in ("classic", "showdown"):
        cfg["budget"][mode]["p_lose80_max"] = 1e-9
    monkeypatch.setattr(ob, "load_risk_config", lambda *a, **k: cfg)
    r = _run(tmp_path, "showdown")
    assert r.ok and r.manifest["versions"][-1]["phase"] == "S"
    s = r.manifest["scenario"]
    assert "least-risk" in s["chosen"]["reason"]
    risks = [p["p_lose80"] for p in s["frontier_all"]]
    chosen = next(p for p in s["frontier_all"] if p["kappa"] == s["chosen"]["kappa"])
    assert chosen["p_lose80"] == min(risks)
    rep = check_file(r.manifest["versions"][-1]["path"], r.run.inputs / "DKSalaries.csv", r.run.inputs / "DKEntries.csv")
    assert rep.ok
