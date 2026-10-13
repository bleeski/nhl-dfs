"""C22 chalk core (flag 95, docs/experiments/chalk_core_2026-10-13.md): the core, the band anchor the fill records, the
counts the preregistration decides on, and the exemption key that no caller passes yet."""

import json
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from nhl_dfs.build import exposure
from nhl_dfs.build import objectives as ob
from nhl_dfs.build import portfolio as pf
from nhl_dfs.build import tiebreak as tb
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.models import contests as contests_mod
from pool_builder import classic_pool, showdown_pool
from test_portfolio import contest, disjoint_lineups, one_field

pytestmark = pytest.mark.c22

RISK = ob.load_risk_config()
FAM = contests_mod.load_contest_families()
EXPO = exposure.load_exposure_config()


@dataclass
class E:
    entry_id: str
    contest_id: str


# -- the core ---------------------------------------------------------------------------------------------------------

def test_core_is_the_two_skaters_with_the_largest_mean_times_ownership_and_never_a_goalie():
    pool = classic_pool()
    sk = [r for r in pool.rows if not r.is_goalie]
    g = next(r for r in pool.rows if r.is_goalie)
    mean = {sk[0].role_id: 200.0, sk[1].role_id: 100.0, sk[2].role_id: 300.0, sk[3].role_id: 150.0, g.role_id: 900.0}
    own = {sk[0].role_id: 30.0, sk[1].role_id: 50.0, sk[2].role_id: 10.0, sk[3].role_id: 40.0, g.role_id: 40.0}
    # products: sk0 6000, sk1 5000, sk2 3000, sk3 6000, goalie 36000 (left out)
    core = tb.chalk_core(mean, own, pool)
    assert core == tuple(sorted([sk[0].person_key, sk[3].person_key]))  # a tie at 6000 goes by person key
    assert g.person_key not in core
    assert tb.chalk_core(mean, {}, pool) == ()  # no ownership, no core
    assert tb.chalk_core(mean, own, pool, n=1) == (sorted([sk[0].person_key, sk[3].person_key])[0],)


def test_core_held_counts_persons_and_a_showdown_pool_has_no_core():
    pool = classic_pool()
    sk = [r for r in pool.rows if not r.is_goalie]
    core = (sk[0].person_key, sk[1].person_key)
    assert tb.core_held([sk[0].role_id, sk[1].role_id, sk[5].role_id], core, pool) == 2
    assert tb.core_held([sk[1].role_id, sk[5].role_id], core, pool) == 1
    assert tb.core_held([sk[5].role_id], core, pool) == 0
    sd = showdown_pool()
    assert tb.chalk_core({r.role_id: 100.0 for r in sd.rows}, {r.role_id: 20.0 for r in sd.rows}, sd) == ()


# -- the exemption key: never passed by the fill ------------------------------------------------------------------

def _items(rng, n):
    return [tb.Item(f"k{i:02d}", rng.uniform(0.9, 1.0), rng.uniform(0, 0.02), rng.uniform(0, 200), rng.uniform(-30, 0),
                    core=rng.randint(0, 2)) for i in range(n)]


def test_with_the_exemption_the_band_never_drops_a_core_piece_the_anchor_holds():
    rng = random.Random(22)
    for trial in range(300):
        items = _items(rng, rng.randint(2, 25))
        for policy in ("own_then_dup", "dup_first"):
            ranked = tb.rank(items, RISK, policy, keep_core=True)
            assert sorted(r.item.key for r in ranked) == sorted(i.key for i in items)
            for b in {r.band_index for r in ranked}:
                members = [r.item for r in ranked if r.band_index == b]
                anchor = min(members, key=lambda it: (-it.value, it.key))  # the band's value-best member
                assert members[0].core >= anchor.core, (trial, policy, b)
            assert ranked[0].item.core >= tb.rank(items, RISK, "mean")[0].item.core


def test_without_the_exemption_rank_is_unchanged_and_ignores_the_core():
    rng = random.Random(23)
    for _ in range(200):
        items = _items(rng, rng.randint(2, 25))
        flat = [tb.Item(i.key, i.value, i.se, i.own, i.dup) for i in items]  # the same items with no core
        for policy in tb.POLICIES:
            a = [(r.item.key, r.band, r.band_index, r.anchor) for r in tb.rank(items, RISK, policy)]
            b = [(r.item.key, r.band, r.band_index, r.anchor) for r in tb.rank(flat, RISK, policy)]
            c = [(r.item.key, r.band, r.band_index, r.anchor) for r in tb.rank(items, RISK, policy, keep_core=False)]
            assert a == b == c


def test_the_exemption_changes_the_pick_exactly_when_the_tie_break_drops_a_core_piece():
    rng = random.Random(24)
    changed = 0
    for _ in range(400):
        items = _items(rng, rng.randint(2, 25))
        for policy in ("own_then_dup", "dup_first"):
            today = tb.rank(items, RISK, policy)[0].item
            anchor = tb.rank(items, RISK, "mean")[0].item
            kept = tb.rank(items, RISK, policy, keep_core=True)[0].item
            is_d = today.core < anchor.core
            assert (kept.key != today.key) == is_d
            changed += is_d
    assert changed > 0  # the identity was exercised, not only its trivial side


# -- the fill records the anchor; the report counts it ------------------------------------------------------------

def _split_scenarios(pool, a, b, f, S, rng):
    """a beats the one opponent f in every draw and b in 99% of them, so a is the band's anchor and b sits inside its band;
    a's points sit on its first two roles, b's and the field's on their first role."""
    ids = [r.role_id for r in pool.rows]
    col = {r: i for i, r in enumerate(ids)}
    fld = rng.integers(300, 700, S)
    base = np.zeros((S, len(ids)), np.int32)
    tot_a = fld + 2
    base[:, col[a.role_ids[0]]] = tot_a // 2
    base[:, col[a.role_ids[1]]] = tot_a - tot_a // 2
    base[:, col[b.role_ids[0]]] = np.where(rng.random(S) < 0.01, fld - 1, fld + 1)
    base[:, col[f.role_ids[0]]] = fld
    return ob.ScenarioSet(ids, base, "selection", 1)


def _select(family, own):
    pool = classic_pool()
    a, b, f = disjoint_lineups(pool, 3)
    scen = _split_scenarios(pool, a, b, f, 2000, np.random.default_rng(5))
    caps = exposure.caps(EXPO, 1, pool, Mode.CLASSIC, 1, budget=RISK["budget"]["classic"])
    ct = {"C": contest("C", family, 2, [180])}
    sel = pf.select([a, b], scen, {"C": one_field(f)}, ct, [E("e1", "C")], caps, pf.RiskBudget(1.0, None, None, None),
                    seed=1, pool=pool, fam_cfg=FAM, risk_cfg=RISK, own_by_contest={"C": own})
    rows, summary = pf.core_report(sel.choices, {c.key: c.role_ids for c in (a, b)}, ct, {"C": own}, scen.means_tenths(),
                                   pool, FAM)
    return pool, a, b, sel, rows, summary


def test_the_fill_records_the_anchor_and_the_report_counts_a_move_off_the_core():
    pool = classic_pool()
    a, b, _ = disjoint_lineups(pool, 3)
    own = {a.role_ids[0]: 60.0, a.role_ids[1]: 50.0, b.role_ids[0]: 5.0}  # the core is a's two scorers; b is the low-owned pick
    _, a, b, sel, rows, summary = _select("large_gpp", own)
    ch = sel.choices["e1"]
    assert ch.key == b.key and ch.anchor_key == a.key  # own_then_dup took the lower ownership inside the band
    assert ch.anchor_own == pytest.approx(110.0) and ch.own_pct == pytest.approx(5.0)
    assert rows["e1"] == {"core_held": 0, "core_held_untied": 2, "tiebreak_moved": "own"}
    assert summary["counts"]["large_gpp"] == {"N": 1, "M": 1, "M_own": 1, "M_dup": 0, "D": 1, "U": 0}
    assert summary["retention"] == {"picks": {"both": 0, "one": 0, "none": 1}, "anchors": {"both": 1, "one": 0, "none": 0}}
    assert summary["retention_by_family"] == {"large_gpp": {"both": 0, "one": 0, "none": 1}}
    assert [p["person"] for p in summary["contests"]["C"]["core"]] == sorted(
        [pool.by_role_id[r].person_key for r in a.role_ids[:2]], key=lambda pk: -own[pool.persons[pk].classic.role_id])
    assert summary["eligible_contests"] == ["C"] and summary["cores_agree"]


def test_a_cash_pick_is_never_moved_and_never_counted():
    pool = classic_pool()
    a, b, _ = disjoint_lineups(pool, 3)
    own = {a.role_ids[0]: 60.0, a.role_ids[1]: 50.0, b.role_ids[0]: 5.0}
    _, a, b, sel, rows, summary = _select("cash", own)
    ch = sel.choices["e1"]
    assert ch.key == ch.anchor_key == a.key
    assert rows["e1"]["tiebreak_moved"] == "" and rows["e1"]["core_held"] == rows["e1"]["core_held_untied"] == 2
    assert summary["counts"] == {} and summary["total"]["N"] == 0
    assert summary["retention_by_family"] == {"cash": {"both": 1, "one": 0, "none": 0}}  # retention is reported for every family


# -- the script: the preregistered verdict, then the whole run in a subprocess --------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _c22():
    sys.path.insert(0, str(ROOT / "scripts"))
    import c22_chalk_core

    return c22_chalk_core


def _run(N, D, ok=True):
    return {"ok": ok, "N": N, "M": D, "M_own": D, "M_dup": 0, "D": D, "U": 0}


def test_the_verdict_follows_the_preregistered_rule():
    c22 = _c22()
    assert c22.slate_verdict([_run(30, 4)] * 5, 5)["verdict"] == "WARRANTED"  # 20 of 150, D in every seed
    assert c22.slate_verdict([_run(30, 0)] + [_run(30, 5)] * 4, 5)["verdict"] == "WARRANTED"  # D in 4 of 5 seeds is enough
    assert c22.slate_verdict([_run(30, 0)] * 2 + [_run(30, 8)] * 3, 5)["verdict"] == "INCONCLUSIVE"  # 24 of 150 but D in 3 seeds
    assert c22.slate_verdict([_run(30, 1)] * 4 + [_run(30, 0)], 5)["verdict"] == "REJECT"  # 4 of 150 is under 0.03
    assert c22.slate_verdict([_run(30, 1)] * 5, 5)["verdict"] == "INCONCLUSIVE"  # 5 of 150 sits between the two lines
    assert c22.slate_verdict([_run(0, 0)] * 5, 5)["verdict"].startswith("NO VERDICT")
    assert c22.slate_verdict([_run(30, 4)] * 4 + [_run(30, 4, ok=False)], 5)["verdict"].startswith("NO VERDICT")
    assert c22.slate_verdict([_run(30, 4)] * 2, 2)["verdict"].startswith("NOT THE PREREGISTERED SAMPLE")
    real = lambda v: {"synthetic": False, "cloned": {"verdict": v}}  # noqa: E731
    assert c22.study_verdict([real("WARRANTED"), real("WARRANTED")]) == "WARRANTED"
    assert c22.study_verdict([real("REJECT"), real("REJECT")]) == "REJECT"
    assert c22.study_verdict([real("REJECT"), real("WARRANTED")]) == "INCONCLUSIVE"
    assert c22.study_verdict([real("WARRANTED")]).startswith("NOT MEASURED")
    assert c22.study_verdict([real("REJECT"), real("REJECT"), {"synthetic": True, "cloned": {"verdict": "REJECT"}}]).startswith(
        "NOT MEASURED on real caches")  # a synthetic slate never reads as REJECT


@pytest.mark.timeout(900)
def test_the_script_runs_the_bed_reproducibly_under_the_write_guard(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "c22_chalk_core.py"), "--out", str(tmp_path), "--small", "--seeds", "1"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"}, cwd=ROOT, timeout=900)
    assert r.returncode == 0, (r.stdout[-1500:] + r.stderr[-1500:])
    guard = json.loads((tmp_path / "guard.json").read_text(encoding="utf-8"))
    assert guard["network_blocked"] == []
    assert [w for w in guard["writes_refused"] if "observations" not in w] == []
    res = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    summary = res["summary"]
    assert summary["verdict"].startswith("NOT MEASURED on real caches")
    slate = summary["slates"][0]
    assert slate["synthetic"] is True and slate["entries_cloned_to"] == 40
    assert slate["noise"] == {"identical_lineups": True, "identical_counts": True}  # the measurement is reproducible
    assert slate["cloned"]["N"] == 40 and all(run["ok"] for run in res["runs"])
    info = slate["information_only"]
    assert sum(info["core_retention_without_the_ownership_order"].values()) == 40  # the mean arm's 40 picks, all large_gpp
    mean_run = next(run for run in res["runs"] if run["arm"] == "mean")
    assert mean_run["N"] == 0 and {c["policy"] for c in mean_run["core"].values()} == {"mean"}
    assert "NOT MEASURED" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_the_real_slate_measurement_runs_when_a_saved_run_is_given(tmp_path):
    saved = os.environ.get("NHL_DFS_C22_SAVED_RUN")
    if not saved:
        pytest.skip("NHL_DFS_C22_SAVED_RUN is not set: the real-slate measurement did NOT run here (backlog B126); a skip is not a pass")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "c22_chalk_core.py"), "--out", str(tmp_path), "--saved-run", saved,
                        "--seeds", "1", "--no-info"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"}, cwd=ROOT, timeout=7200)
    assert r.returncode == 0, (r.stdout[-1500:] + r.stderr[-1500:])
    slate = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))["summary"]["slates"][0]
    assert slate["synthetic"] is False and slate["cloned"]["N"] > 0, slate["cloned"]
