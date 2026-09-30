"""C10: the deterministic QA controller."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np
import pytest

from nhl_dfs.build import controller
from nhl_dfs.build.state import open_run
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary
from conftest import TESTS
from nhl_dfs.build.run import run_slate

LS = TESTS / "fixtures" / "late_swap" / "classic"  # two goalies per team; AAA@BBB 23:00Z, CCC@DDD 00:00Z, EEE@FFF 02:00Z
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
MID = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # AAA@BBB started
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


@pytest.fixture(scope="module")
def full_classic(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c10_full")
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=False, scenario=True,
                  scenario_n=SMALL, out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: BEFORE)
    assert r.ok and r.manifest["scenario"]["version"], r.manifest["failed"]
    return tmp, r

pytestmark = pytest.mark.c10


def _copy(full_classic, tmp_path):  # noqa: F811
    tmp, r = full_classic
    runs = tmp_path / "runs"
    shutil.copytree(r.run.path, runs / r.run.run_id)
    return open_run(runs, r.run.run_id), runs, tmp_path / "outputs"


def _current(run):
    return {e.entry_id: [cell_role_id(c) for c in e.cells if cell_role_id(c)]
            for e in read_entries(run.version_file(run.current_version())).entries}


def _apply(run, runs, outs, text, now=BEFORE, rnd=1, **kw):
    return controller.apply_round(run, rnd, text, now=now, runs_root=runs, outputs_root=outs, **kw)


def _goalie_case(run):
    pool = read_salary(run.inputs / "DKSalaries.csv")
    lus = _current(run)
    eid, x = next((e, r) for e, lu in lus.items() for r in lu if pool.by_role_id[r].is_goalie)
    team = pool.by_role_id[x].team
    y = next(r for r in pool.rows if r.is_goalie and r.team == team and r.person_key != pool.by_role_id[x].person_key)
    return pool, eid, x, y


def _override(role_id, field, old, new, now=BEFORE, conf=0.9):
    return {"type": "override", "role_id": role_id, "nhl_id": None, "game_id": None, "field": field, "old": old, "new": new,
            "effective_utc": (now - timedelta(hours=1)).isoformat(), "expiry_utc": (now + timedelta(hours=6)).isoformat(),
            "source_url": "https://www.dailyfaceoff.com/starting-goalies", "claim": "team confirmed the starter",
            "confidence": conf}


def test_malformed_proposals_end_qa_and_keep_the_incumbent(full_classic, tmp_path):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    before = run.version_file(run.current_version()).read_bytes()
    res = _apply(run, runs, outs, "Sure! Here are my thoughts: swap McDavid")
    assert res.published_version is None and not res.another_round and "malformed" in res.stop_reason
    assert run.version_file(run.current_version()).read_bytes() == before
    rec = json.loads((run.path / "qa" / "round_1.json").read_text(encoding="utf-8"))
    assert rec["proposals_sha256"] and rec["another_round"] is False
    again = _apply(run, runs, outs, "[]")
    assert "already applied" in again.stop_reason  # a round is never applied twice


def test_a_correctness_repair_is_accepted_without_a_simulation_contest(full_classic, tmp_path, monkeypatch):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    pool, eid, x, y = _goalie_case(run)
    monkeypatch.setattr(controller, "evaluate", lambda *a, **k: pytest.fail("a correctness repair must not be contested"))
    reply = json.dumps({"packet_id": "p", "proposals": [
        {"kind": "correctness", "target": {"role_id": y.role_id}, "change": _override(y.role_id, "goalie_start", False, True),
         "evidence": "DF confirmed", "source_url": "https://www.dailyfaceoff.com/starting-goalies"}]})
    res = _apply(run, runs, outs, reply)
    assert res.accepted_correctness == 1 and res.decisions[0]["status"] == "accepted"
    assert res.published_version is not None and res.another_round  # rounds 2-3 only after a correctness repair
    after = _current(run)
    assert x not in after[eid]  # the non-starter left every open cell (B17 on the controller path)
    assert all(x not in lu for lu in after.values())
    stored = json.loads((run.path / "news" / "accepted_overrides.json").read_text(encoding="utf-8"))
    assert stored[0]["role_id"] == y.role_id


def test_an_invalid_or_weak_correctness_claim_is_rejected(full_classic, tmp_path):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    pool, eid, x, y = _goalie_case(run)
    bad_old = _override(y.role_id, "goalie_start", True, True)  # old value does not match the state
    weak = _override(y.role_id, "goalie_start", False, True, conf=0.3)
    reply = json.dumps([{"kind": "correctness", "target": {}, "change": bad_old, "evidence": "", "source_url": None},
                        {"kind": "correctness", "target": {}, "change": weak, "evidence": "", "source_url": None}])
    res = _apply(run, runs, outs, reply)
    assert [d["status"] for d in res.decisions] == ["rejected", "rejected"]
    assert "old value" in res.decisions[0]["reason"] and "confidence" in res.decisions[1]["reason"]
    assert res.published_version is None and not res.another_round and "zero changes" in res.stop_reason


def test_a_proposal_touching_a_pinned_cell_is_rejected(full_classic, tmp_path):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    pool = read_salary(run.inputs / "DKSalaries.csv")
    lus = _current(run)
    eid, out_r = next((e, r) for e, lu in lus.items() for r in lu if pool.by_role_id[r].team in ("AAA", "BBB"))
    in_r = next(r.role_id for r in pool.rows if r.position == pool.by_role_id[out_r].position and r.role_id not in lus[eid]
                and r.team in ("EEE", "FFF"))
    reply = json.dumps([{"kind": "strategic", "target": {"entry_id": eid},
                         "change": {"type": "swap", "entry_id": eid, "out_role_id": out_r, "in_role_id": in_r},
                         "evidence": "", "source_url": None}])
    res = _apply(run, runs, outs, reply, now=MID)  # AAA@BBB has started
    assert res.decisions[0]["status"] == "rejected" and "pinned" in res.decisions[0]["reason"]


def test_over_the_limit_and_unsupported_proposals_are_rejected(full_classic, tmp_path):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    reply = json.dumps([{"kind": "strategic", "target": {}, "change": {"type": "cap"}, "evidence": "", "source_url": None}] * 7)
    res = _apply(run, runs, outs, reply)
    assert len(res.decisions) == 7
    assert all("unsupported" in d["reason"] for d in res.decisions[:5])
    assert all("limit" in d["reason"] for d in res.decisions[5:])


def test_a_strategic_change_accepted_by_the_comparison_is_labeled_and_published(full_classic, tmp_path, monkeypatch):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    pool = read_salary(run.inputs / "DKSalaries.csv")
    lus = _current(run)
    eid, out_r = next((e, r) for e, lu in lus.items() for r in lu if not pool.by_role_id[r].is_goalie)
    me = pool.by_role_id[out_r]
    in_r = next(r.role_id for r in pool.rows if r.position == me.position and r.roster_positions == me.roster_positions
                and r.salary <= me.salary and r.role_id not in lus[eid] and r.person_key != me.person_key)
    seen = {}

    def fake(so, before, after, changed, *a, **k):
        seen["changed"] = changed
        return True, {"selection": {"utility_gain": 1.0}}

    monkeypatch.setattr(controller, "evaluate", fake)
    reply = json.dumps([{"kind": "strategic", "target": {"entry_id": eid},
                         "change": {"type": "swap", "entry_id": eid, "out_role_id": out_r, "in_role_id": in_r},
                         "evidence": "leverage", "source_url": None}])
    res = _apply(run, runs, outs, reply)
    assert res.accepted_strategic == 1 and seen["changed"] == {eid}
    assert "unvalidated modeled improvement" in res.decisions[0]["reason"]  # FIELD_CALIBRATION=PRIOR
    assert in_r in _current(run)[eid] and res.published_version is not None
    assert not res.another_round  # a strategic change alone does not open another round


# -- the paired comparison, on crafted draws ----------------------------------------------------------------

def _fake_so(util_before, util_after, tail=(1.0, 1.0), pay=(500, 500)):
    S = len(util_before)

    def joint(cid, lineups, purpose):
        after = lineups[0][0] == "NEW"
        u = np.asarray(util_after if after else util_before, np.int64)[:, None]
        return NS(payout_cents=np.full((S, 1), pay[1] if after else pay[0]), utility_cents=u,
                  exp_payout_top1pct=np.array([tail[1] if after else tail[0]]), exp_payout_top1pct_se=np.array([0.01]))

    return NS(cache=NS(contests={"c": object()}), joint=joint)


RISK = {"budget": {"classic": {"p_lose80_max": 0.6}}, "tiebreak": {"band_pct": 0.03, "se_mult": 1.0}}


def test_a_gain_inside_the_band_is_inconclusive():
    rng = np.random.default_rng(1)
    before = rng.integers(0, 1000, 4000)
    after = before + rng.integers(-3, 4, 4000)  # about zero on average: inside the band
    ok, figs = controller.evaluate(_fake_so(before, after), {"e": ["OLD"]}, {"e": ["NEW"]}, {"e"}, {"e": "c"}, {"e": 10.0},
                                   RISK, Mode.CLASSIC)
    assert not ok and not figs["selection"]["gain_ok"]


def test_a_gain_beyond_the_band_that_is_safe_passes_and_a_tail_loss_does_not():
    rng = np.random.default_rng(2)
    before = rng.integers(0, 1000, 4000)
    after = before + 300
    so = _fake_so(before, after)
    ok, figs = controller.evaluate(so, {"e": ["OLD"]}, {"e": ["NEW"]}, {"e"}, {"e": "c"}, {"e": 10.0}, RISK, Mode.CLASSIC)
    assert ok and figs["selection"]["gain_ok"] and figs["referee"]["tail_ok"]
    worse_tail = _fake_so(before, after, tail=(1.0, 0.5))
    ok2, figs2 = controller.evaluate(worse_tail, {"e": ["OLD"]}, {"e": ["NEW"]}, {"e"}, {"e": "c"}, {"e": 10.0}, RISK, Mode.CLASSIC)
    assert not ok2 and not figs2["selection"]["tail_ok"]
    unsafe = _fake_so(before, after, pay=(500, 100))  # payouts collapse under 20% of fees in every scenario
    ok3, figs3 = controller.evaluate(unsafe, {"e": ["OLD"]}, {"e": ["NEW"]}, {"e"}, {"e": "c"}, {"e": 10.0}, RISK, Mode.CLASSIC)
    assert not ok3 and not figs3["selection"]["safety_ok"]


def test_rounds_two_and_three_need_a_correctness_repair(full_classic, tmp_path):  # noqa: F811
    run, runs, outs = _copy(full_classic, tmp_path)
    cfg = controller.packet_mod.load_qa_config()
    assert controller.permitted(run, 2, cfg, BEFORE, runs)[0] is False
    _apply(run, runs, outs, "[]")
    ok, why = controller.permitted(run, 2, cfg, BEFORE, runs)
    assert not ok and "did not permit" in why
    late = datetime(2026, 10, 16, 1, 55, tzinfo=timezone.utc)  # EEE@FFF at 02:00: inside T-8
    run2, runs2, outs2 = _copy(full_classic, tmp_path / "b")
    ok3, why3 = controller.permitted(run2, 1, cfg, late, runs2)
    assert not ok3 and ("T-8" in why3 or "nothing open" in why3)
