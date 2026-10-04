"""C14: lock-safe publishing (B52 and B53; reviews R01 and R02).

No publication path commits a cell from a game that has started or crossed the edit stop. The fixture is the
late-swap Classic slate: AAA@BBB starts 23:00Z, CCC@DDD 00:00Z, EEE@FFF 02:00Z, and the 300 s edit-stop buffer
pins AAA@BBB's cells from 22:55:00Z. Time moves the way it did in the reviewer's reproductions: a patched wall
clock and a wrapped solver or builder advance it in the middle of the work. No test hands the code under test a
clock that would hide the defect.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import TESTS
from nhl_dfs.build import controller, late_swap
from nhl_dfs.build.run import run_slate
from nhl_dfs.build.state import FileLock, LockCrossed, LockTimeout, PublishRefused, open_run, publish, sha256
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c14

LS = TESTS / "fixtures" / "late_swap" / "classic"
EARLY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
T_ROUND = datetime(2026, 10, 15, 22, 50, 0, tzinfo=timezone.utc)  # T-10 of AAA@BBB: a round or run may start
T_EDIT_STOP = datetime(2026, 10, 15, 22, 56, 0, tzinfo=timezone.utc)  # AAA@BBB is inside the 300 s buffer
T_STARTED = datetime(2026, 10, 15, 23, 0, 1, tzinfo=timezone.utc)  # AAA@BBB has started


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c14_base")
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=True,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: EARLY)
    assert r.ok, r.manifest["failed"]
    return tmp, r


def _copy(baseline, tmp_path):  # noqa: F811
    tmp, r = baseline
    shutil.copytree(r.run.path, tmp_path / "runs" / r.run.run_id)
    shutil.copytree(tmp / "outputs", tmp_path / "outputs")
    return open_run(tmp_path / "runs", r.run.run_id), tmp_path / "runs", tmp_path / "outputs", r.slate_id


def _state(run, outs, slate_id):
    """What an unchanged incumbent looks like: the current pointer, every version, its bytes, the public bytes."""
    v = run.current_version()
    return (v, run.version_numbers(), sha256(run.version_file(v).read_bytes()),
            sha256((outs / slate_id / "DKEntries.csv").read_bytes()))


def _early_game_skater(run) -> str:
    pool = read_salary(run.inputs / "DKSalaries.csv")
    for e in read_entries(run.version_file(run.current_version())).entries:
        for c in e.cells:
            row = pool.by_role_id.get(cell_role_id(c))
            if row is not None and row.team in ("AAA", "BBB") and not row.is_goalie:
                return row.role_id
    raise AssertionError("no AAA@BBB skater in the fixture lineups")


def _out_reply(role_id: str, now: datetime) -> str:
    return json.dumps({"overrides": [{
        "type": "override", "role_id": role_id, "nhl_id": None, "game_id": None, "field": "participation",
        "old": "PLAYING", "new": "OUT", "effective_utc": (now - timedelta(hours=1)).isoformat(),
        "expiry_utc": (now + timedelta(hours=6)).isoformat(), "source_url": "https://www.dailyfaceoff.com/teams/aaa/line-combinations",
        "claim": "ruled out of tonight's game", "confidence": 0.95}]})


def _moving_clock(monkeypatch, jump_to: datetime | None, where: str = "solve") -> dict:
    """The controller's wall clock reads T_ROUND until the work at `where` finishes, then jump_to. "solve": the repair solve,
    before the controller's early recheck. "referee": the referee run on the staged file, after the early recheck and
    before the publish, so only the check made under the publish lock can see the boundary."""
    import sys

    box = {"t": T_ROUND, "solves": 0}
    monkeypatch.setattr(controller, "_wall_now", lambda: box["t"])

    def wrap(real):
        def inner(*a, **k):
            out = real(*a, **k)
            box["solves"] += 1
            if jump_to is not None:
                box["t"] = jump_to
            return out
        return inner

    if where == "solve":
        monkeypatch.setattr(late_swap, "_solve_entry", wrap(late_swap._solve_entry))
    else:
        mod = sys.modules["nhl_dfs.referee.check_file"]
        monkeypatch.setattr(mod, "check_file", wrap(mod.check_file))
    return box


def _apply_out(run, runs, outs, rid, shape):
    kw = {"now": T_ROUND} if shape == "now=" else {}  # CLI-style call, or no round-start time at all
    return controller.apply_round(run, 1, _out_reply(rid, T_ROUND), runs_root=runs, outputs_root=outs, source="overrides", **kw)


# -- R01 / B52: the controller's final recheck reads the live clock -----------------------------------------------

@pytest.mark.parametrize("shape", ["now=", "no now"])
@pytest.mark.parametrize("where", ["solve", "referee"])
@pytest.mark.parametrize("jump", [T_EDIT_STOP, T_STARTED], ids=["edit_stop", "locked"])
def test_r01_a_boundary_crossed_during_the_work_publishes_nothing(baseline, tmp_path, monkeypatch, shape, where, jump):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    rid = _early_game_skater(run)
    box = _moving_clock(monkeypatch, jump, where)
    before = _state(run, outs, sid)
    res = _apply_out(run, runs, outs, rid, shape)
    assert res.accepted_correctness == 1 and box["solves"] >= 1, res.stop_reason  # the repair really ran, and time moved
    assert res.published_version is None and "lock boundary was crossed" in res.stop_reason
    assert _state(run, outs, sid) == before  # incumbent pointer, versions and bytes, and the public bytes


@pytest.mark.parametrize("shape", ["now=", "no now"])
def test_r01_before_the_boundary_a_repair_still_publishes(baseline, tmp_path, monkeypatch, shape):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    rid = _early_game_skater(run)
    box = _moving_clock(monkeypatch, None)
    res = _apply_out(run, runs, outs, rid, shape)
    assert box["solves"] >= 1 and res.published_version == 2, res.stop_reason
    assert run.current_version() == 2
    assert rid not in {cell_role_id(c) for e in read_entries(run.version_file(2)).entries for c in e.cells}
    assert sha256((outs / sid / "DKEntries.csv").read_bytes()) == sha256(run.version_file(2).read_bytes())


def test_r01_the_deadline_is_read_from_the_round_start_not_a_later_tick(baseline, tmp_path, monkeypatch):  # noqa: F811
    """T-8 is a start gate: a round that began before it and ends after it is judged by the lock recheck, not the deadline."""
    run, runs, outs, sid = _copy(baseline, tmp_path)
    box = _moving_clock(monkeypatch, datetime(2026, 10, 15, 22, 53, tzinfo=timezone.utc))  # past T-8, before the edit stop
    res = _apply_out(run, runs, outs, _early_game_skater(run), "no now")
    assert box["solves"] >= 1 and res.published_version == 2, res.stop_reason


def test_r01_publish_runs_the_precheck_under_both_locks_and_writes_nothing_when_it_refuses(baseline, tmp_path):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    data = run.version_file(1).read_bytes()
    report = check_file(run.version_file(1), run.inputs / "DKSalaries.csv", run.inputs / "DKEntries.csv")
    before = _state(run, outs, sid)
    held = {}

    def precheck():
        for name, path in (("run", run.lock_path), ("slate", outs / sid / ".lock")):
            try:
                with FileLock(path, timeout_s=0.2):
                    held[name] = False
            except LockTimeout:
                held[name] = True
        return "AAA@BBB started"

    with pytest.raises(LockCrossed, match="AAA@BBB started") as exc:
        publish(run, data, report, sid, outputs_root=outs, precheck=precheck)
    assert isinstance(exc.value, PublishRefused) and held == {"run": True, "slate": True}
    assert _state(run, outs, sid) == before
    assert publish(run, data, report, sid, outputs_root=outs, precheck=lambda: None).version == 2  # a clean precheck publishes


@pytest.mark.parametrize("cmd, args", [("qa-apply", ["--round", "1", "--proposals"]), ("overrides-apply", ["--file"])])
def test_r01_the_cli_freezes_the_clock_only_for_a_labeled_rehearsal(baseline, tmp_path, monkeypatch, capsys, cmd, args):  # noqa: F811
    from nhl_dfs import cli

    run, runs, outs, sid = _copy(baseline, tmp_path)
    reply = tmp_path / "reply.json"
    reply.write_text("[]", encoding="utf-8")
    seen: list[dict] = []
    monkeypatch.setattr(controller, "apply_round", lambda *a, **k: seen.append(k) or NS(
        lines=lambda: ["QA round 1: x"], accepted_correctness=0))
    monkeypatch.setattr(cli, "_goalie_refresh", lambda r: None)
    base = ["--run", run.run_id, "--runs-root", str(runs), "--outputs-root", str(outs)]
    assert cli.main([cmd, *base, *args, str(reply)]) == 0
    assert seen[-1]["now"] is None and seen[-1]["clock"] is None  # production: the controller reads the wall clock itself
    assert cli.main([cmd, *base, *args, str(reply), "--as-of", "2026-10-15T22:50:00Z"]) == 0
    assert seen[-1]["now"] == T_ROUND and seen[-1]["clock"]() == T_ROUND  # rehearsal: the labeled time, explicitly
    capsys.readouterr()
