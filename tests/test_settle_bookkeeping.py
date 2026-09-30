"""Backlog B31 and B30: settling a run and its refresh or late-swap child books each DraftKings entry once and counts a
contest's labels once; a child without its own pre-lock forecast is graded against its nearest pre-lock ancestor."""
import pytest

from nhl_dfs.learn import ledger as lg
from nhl_dfs.learn.settle import evidence_counts

pytestmark = pytest.mark.c11


def _sl(run_id, settled, payouts, fee=100):
    entries = [lg.EntryResult("297000001", "NHL Synthetic Classic", eid, fee, i + 1, None, "50", p, "EXACT", "t")
               for i, (eid, p) in enumerate(payouts.items())]
    return lg.SlateLedger(run_id, "classic-20261015-x", "classic", "2026-10-15", settled, entries)


PAY = {"7100000001": 400, "7100000002": 0, "7100000003": 0}


@pytest.mark.parametrize("order", [("parent", "child"), ("child", "parent")])
def test_a_run_and_its_child_book_each_entry_once_whichever_settles_first(tmp_path, order):
    for i, rid in enumerate(order):
        sl = _sl(rid, f"2026-10-16T0{i}:00:00Z", PAY)
        before = lg.replaced_by(sl, tmp_path)
        lg.append(sl, tmp_path)
        if i == 1:
            assert before == {order[0]: 3}  # the note settle prints names the run it replaced
    df = lg.read(tmp_path)
    assert len(df) == 3 and set(df["run_id"]) == {order[1]}  # one row per entry, from the newest settle
    dd = lg.drawdown(tmp_path)
    assert dd["runs"] == 1 and dd["cum_net_known_cents"] == 400 - 300  # fees booked once


def test_the_same_run_settled_twice_replaces_its_own_rows(tmp_path):
    lg.append(_sl("parent", "2026-10-16T00:00:00Z", PAY), tmp_path)
    sl = _sl("parent", "2026-10-16T01:00:00Z", PAY)
    assert lg.replaced_by(sl, tmp_path) == {}
    lg.append(sl, tmp_path)
    assert len(lg.read(tmp_path)) == 3
    lg.append(_sl("other", "2026-10-16T02:00:00Z", {"7100000009": 0}), tmp_path)  # a different entry stays booked
    assert len(lg.read(tmp_path)) == 4


def test_a_contest_settled_on_two_runs_counts_its_labels_once():
    def entry(rid, when):
        return {"run_id": rid, "mode": "classic", "slate_date": "2026-10-15", "slate_id": "s1", "games": ["A@B"],
                "frozen": True, "complete_payout": True, "settled_utc": when,
                "contests": {"297000001": {"family": "large_gpp", "labels": 250}},
                "played": [["2026-10-15", "A@B", "p|A|F", "F"]]}

    one = evidence_counts({"parent": entry("parent", "2026-10-16T00:00:00Z")}, "classic")
    both = evidence_counts({"parent": entry("parent", "2026-10-16T00:00:00Z"),
                            "child": entry("child", "2026-10-16T01:00:00Z")}, "classic")
    assert both.ownership_labels == one.ownership_labels == 250
    assert (both.slate_groups, both.skater_games) == (one.slate_groups, one.skater_games) == (1, 1)


# -- B30: a child without its own pre-lock forecast grades against its nearest pre-lock ancestor ----------------------

import json  # noqa: E402
import os  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)  # first game of the late-swap fixture
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def _chain(tmp_path):
    from conftest import TESTS
    from nhl_dfs.build import late_swap
    from nhl_dfs.build.run import run_slate

    ls = TESTS / "fixtures" / "late_swap" / "classic"
    parent = run_slate(ls / "DKSalaries.csv", ls / "DKEntries.template.csv", offline=True, baseline_only=False,
                       scenario=True, scenario_n=SMALL, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                       clock=lambda: BEFORE)
    assert parent.ok and (parent.run.path / "scenario" / "meta.json").exists(), parent.manifest["failed"]
    child = late_swap.run(parent.run.run_id, ls / "DKEntries.current.csv", offline=True, fast=True,
                          runs_root=tmp_path / "runs", outputs_root=tmp_path / "outputs", as_of=G1 + timedelta(minutes=10))
    assert child.statuses["FILE_VALID"] == "TRUE" and not (child.run.path / "scenario").exists(), child.messages
    return parent, child


def _settle(tmp_path, run_id, **kw):
    from nhl_dfs.learn import settle
    from test_frozen_record import _standings

    sdir = tmp_path / "standings"
    if not sdir.exists():
        _standings(tmp_path)
    return settle.run(run_id, sdir, runs_root=tmp_path / "runs", ledger_root=tmp_path / "ledger",
                      backlog_path=tmp_path / "BACKLOG.md", offline=True, boxscores=False, **kw)


def _after_first_game(run_path):
    t = (G1 + timedelta(hours=1)).timestamp()
    for name in ("field.json", "scenario/meta.json", "scenario/fields.json"):
        p = run_path / name
        if p.exists():
            os.utime(p, (t, t))


def test_a_late_swap_child_grades_from_its_pre_lock_ancestor_and_keeps_its_own_money(tmp_path):
    parent, child = _chain(tmp_path)
    rec = _settle(tmp_path, child.run.run_id)
    f = rec["forecast"]
    assert f["status"] == "PRE_LOCK" and f["run_id"] == parent.run.run_id
    assert f"graded from run {parent.run.run_id}" in f["detail"] and "money and entered lineups from this run" in f["detail"]
    assert rec["forecasts"] is not None and rec["ownership"]  # graded from the ancestor's frozen files
    from nhl_dfs.learn.settle import frozen_hashes

    assert rec["freeze_check"]["ok"]  # the ancestor's frozen files are in the freeze check too
    assert rec["freeze_check"]["files"] == len(frozen_hashes(child.run)) + len(frozen_hashes(parent.run))
    idx = json.loads((tmp_path / "ledger" / "graded.json").read_text(encoding="utf-8"))
    assert idx[child.run.run_id]["frozen"] is True and idx[child.run.run_id]["forecast_run_id"] == parent.run.run_id
    assert rec["ledger"]["run_id"] == child.run.run_id  # money is the child's


def test_a_child_cache_written_after_the_first_game_is_passed_over_for_the_pre_lock_ancestor(tmp_path):
    from nhl_dfs.build import refresh

    parent, _ = _chain(tmp_path)
    f = refresh.run(parent.run.run_id, offline=True, runs_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                    as_of=BEFORE)
    assert f.ok and (f.run.path / "scenario" / "meta.json").exists()
    _after_first_game(f.run.path)
    rec = _settle(tmp_path, f.run.run_id)
    assert rec["forecast"]["run_id"] == parent.run.run_id and rec["forecast"]["own_status"] == "POST_LOCK"
    assert rec["forecast"]["status"] == "PRE_LOCK"


def test_with_no_pre_lock_run_on_the_chain_the_run_grades_itself_as_before(tmp_path):
    parent, child = _chain(tmp_path)
    _after_first_game(parent.run.path)
    rec = _settle(tmp_path, child.run.run_id)
    assert rec["forecast"]["run_id"] == child.run.run_id and "graded from run" not in rec["forecast"]["detail"]
    assert rec["forecasts"] is None  # the child saved no scenario cache: not graded, exactly as before


def test_settling_the_ancestor_and_the_child_counts_once(tmp_path):
    from nhl_dfs.learn.settle import evidence_counts

    parent, child = _chain(tmp_path)
    a = _settle(tmp_path, parent.run.run_id)
    b = _settle(tmp_path, child.run.run_id)
    assert any(f"replaces 5 row(s) of run {parent.run.run_id}" in n for n in b["notes"])
    assert len(lg.read(tmp_path / "ledger")) == 5
    idx = json.loads((tmp_path / "ledger" / "graded.json").read_text(encoding="utf-8"))
    both = evidence_counts(idx, "classic")
    one = evidence_counts({parent.run.run_id: idx[parent.run.run_id]}, "classic")
    assert both.ownership_labels == one.ownership_labels > 0 and both.slate_groups == one.slate_groups == 1
    assert a["forecast"]["run_id"] == b["forecast"]["run_id"] == parent.run.run_id
