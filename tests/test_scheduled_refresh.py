"""B23: the scheduled pre-lock refresh dispatcher, rehearsed with pinned clocks and a fake notifier (nothing is
registered with Task Scheduler and no toast is shown)."""
from datetime import timedelta

import pytest

from nhl_dfs.build import refresh, scheduled
from test_goalie_gate import BEFORE, G1, _fake_codes, base, inject, report  # noqa: F401  (autouse fixture)

pytestmark = pytest.mark.c9

CFG = {"windows_min": [60, 20], "late_cutoff_min": 5, "horizon_h": 18, "notify": {"toast": False, "on_no_change": False}}


def _dispatch(tmp_path, now, notes, refresh_fn=None):
    runs = tmp_path / "runs"

    def ref(run_id):
        return refresh.run(run_id, offline=True, runs_root=runs, as_of=now, objective="baseline")

    return scheduled.dispatch(now=now, runs_root=runs, cfg=CFG, refresh_fn=refresh_fn or ref,
                              notify_fn=lambda t, m: notes.append((t, m)) or "fake")


def _first_goalie(b):
    from nhl_dfs.build import goalies
    from nhl_dfs.intake.salary import read_salary

    pool = read_salary(b.run.inputs / "DKSalaries.csv")
    lu = goalies.lineups_of(b.run.version_file(b.run.current_version()), pool)
    g = next(pool.by_role_id[x] for x in next(iter(lu.values())) if pool.by_role_id[x].is_goalie)
    other = next(r for r in pool.rows if r.is_goalie and r.team == g.team and r.person_key != g.person_key)
    return g, other


def test_a_later_confirmation_changes_the_file_and_notifies_before_t15(tmp_path, monkeypatch):
    b = base(tmp_path)
    g, other = _first_goalie(b)
    notes = []
    assert _dispatch(tmp_path, G1 - timedelta(minutes=90), notes) == []  # before T-60: nothing is due
    now = G1 - timedelta(minutes=58)
    inject(monkeypatch, [report(g.team, other.name, created=now - timedelta(minutes=10))])
    ev = _dispatch(tmp_path, now, notes)
    assert len(ev) == 1 and ev[0]["window"] == "T-60" and ev[0]["result"] == "ok"
    assert ev[0]["changed_cells"] >= 1 and ev[0]["notified"] == "fake"
    title, body = notes[-1]
    assert "file changed" in title and "late swap" in body and now < G1 - timedelta(minutes=15)
    assert _dispatch(tmp_path, now + timedelta(minutes=5), notes) == []  # each window runs once
    log = (tmp_path / "runs" / "_scheduler" / "notifications.log").read_text(encoding="utf-8")
    assert "file changed" in log


def test_windows_run_once_each_and_nothing_runs_inside_the_late_cutoff(tmp_path, monkeypatch):
    b = base(tmp_path)
    inject(monkeypatch, [])
    notes, seen = [], []

    def fake_refresh(run_id):
        seen.append(run_id)
        return refresh.run(run_id, offline=True, runs_root=tmp_path / "runs", as_of=BEFORE, objective="baseline")

    assert [e["window"] for e in _dispatch(tmp_path, G1 - timedelta(minutes=60), notes, fake_refresh)] == ["T-60"]
    assert [e["window"] for e in _dispatch(tmp_path, G1 - timedelta(minutes=19), notes, fake_refresh)] == ["T-20"]
    assert _dispatch(tmp_path, G1 - timedelta(minutes=10), notes, fake_refresh) == []
    assert seen == [b.run.run_id] * 2 and notes == []  # nothing changed and goalie news is not required here


def test_a_late_wake_runs_one_refresh_and_closes_both_windows(tmp_path, monkeypatch):
    base(tmp_path)
    inject(monkeypatch, [])
    notes = []
    ev = _dispatch(tmp_path, G1 - timedelta(minutes=15), notes)
    assert [e["window"] for e in ev] == ["T-20"] and ev[0]["closes"] == ["T-60", "T-20"]
    assert _dispatch(tmp_path, G1 - timedelta(minutes=12), notes) == []


def test_a_failed_refresh_notifies(tmp_path, monkeypatch):
    base(tmp_path)
    notes = []

    def boom(run_id):
        raise RuntimeError("draftables timed out")

    ev = _dispatch(tmp_path, G1 - timedelta(minutes=40), notes, boom)
    assert ev[0]["result"] == "failed" and "FAILED" in notes[-1][0] and "previous file stands" in notes[-1][1]


def test_rehearsal_children_and_undelivered_runs_are_not_candidates(tmp_path, monkeypatch):
    b = base(tmp_path)
    inject(monkeypatch, [])
    refresh.run(b.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=BEFORE, objective="baseline")
    got = scheduled.candidates(tmp_path / "runs", G1 - timedelta(minutes=30), 18)
    assert [c.run_id for c in got] == [b.run.run_id]  # the rehearsal child is skipped
    assert scheduled.candidates(tmp_path / "runs", G1 + timedelta(minutes=1), 18) == []  # the lock has passed
