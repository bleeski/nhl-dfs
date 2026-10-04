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
from nhl_dfs.build import locks as locks_mod
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.assign import Assignment
from nhl_dfs.build.late_swap import _canonical, splice_cells
from nhl_dfs.build.run import load_runtime_config, run_slate
from nhl_dfs.build.state import FileLock, LockCrossed, LockTimeout, PublishRefused, open_run, publish, sha256
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, check_lineup, slot_accepts
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c14

LS = TESTS / "fixtures" / "late_swap" / "classic"
EARLY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
T_ROUND = datetime(2026, 10, 15, 22, 50, 0, tzinfo=timezone.utc)  # T-10 of AAA@BBB: a round or run may start
T_EDIT_STOP = datetime(2026, 10, 15, 22, 56, 0, tzinfo=timezone.utc)  # AAA@BBB is inside the 300 s buffer
T_STARTED = datetime(2026, 10, 15, 23, 0, 1, tzinfo=timezone.utc)  # AAA@BBB has started
T_MARGIN = datetime(2026, 10, 15, 22, 54, 40, tzinfo=timezone.utc)  # T-5:20: before the edit stop, inside the optional-work margin
EARLY_GAME, OPEN_GAMES = {"AAA", "BBB"}, {"CCC", "DDD", "EEE", "FFF"}  # the teams of AAA@BBB, and of the two later games
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}
JUMPS = pytest.mark.parametrize("jump", [T_EDIT_STOP, T_STARTED], ids=["edit_stop", "locked"])


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


# -- R02 / B53: every initial-run publish rechecks the lock state -------------------------------------------------

def _slate(tmp_path, box, *, fixture="classic", **kw):
    """run_slate on a fixture slate with a clock the test moves (box[\"t\"]); offline, so there is no network phase."""
    src = TESTS / "fixtures" / "late_swap" / fixture
    return run_slate(src / "DKSalaries.csv", src / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                     outputs_root=tmp_path / "outputs", clock=lambda: box["t"], **kw)


def _jump_after_build_bank(monkeypatch, box, to):
    """The reviewer's reproduction: the candidate build finishes, then time passes, then the file is written and published."""
    real = run_mod.build_bank

    def build(*a, **k):
        out = real(*a, **k)
        box["t"] = to
        return out

    monkeypatch.setattr(run_mod, "build_bank", build)


def _jump_around_publish(monkeypatch, box, phase, to, when):
    """Move the clock just before or just after phase `phase` publishes (A, B, P or S)."""
    real = run_mod._export_and_publish

    def publish_phase(*a, **k):
        if k["phase"] == phase and when == "before":
            box["t"] = to
        out = real(*a, **k)
        if k["phase"] == phase and when == "after":
            box["t"] = to
        return out

    monkeypatch.setattr(run_mod, "_export_and_publish", publish_phase)


def _public(tmp_path, r):
    return tmp_path / "outputs" / r.slate_id / "DKEntries.csv"


def test_r02_the_fixture_has_no_in_progress_markers():
    assert b"In-Progress" not in (LS / "DKSalaries.csv").read_bytes()  # these tests do not lean on B42's intake rows


def test_r02_phase_a_a_game_starting_during_the_build_publishes_nothing(tmp_path, monkeypatch):
    box = {"t": T_ROUND}
    _jump_after_build_bank(monkeypatch, box, T_STARTED)
    r = _slate(tmp_path, box)
    assert not r.ok and r.run.version_numbers() == [] and r.public_path is None
    assert not _public(tmp_path, r).exists()
    assert any("a game started while the file was being built" in x and "late-swap" in x for x in r.messages)
    assert r.manifest["lock_stops"][0]["pass"] == "phase A publish" and r.statuses["FILE_VALID"] == "FALSE"
    assert "late-swap" in r.manifest["recommendation"] and "rerun cannot help" in r.manifest["recommendation"]


def test_r02_phase_a_inside_the_edit_stop_ships_with_a_warning_flag_20(tmp_path, monkeypatch):
    box = {"t": T_ROUND}
    _jump_after_build_bank(monkeypatch, box, T_EDIT_STOP)
    r = _slate(tmp_path, box)
    assert r.ok and r.run.version_numbers() == [1] and _public(tmp_path, r).exists()
    assert any("inside the edit stop" in x and "published as built" in x for x in r.messages)
    assert "lock_stops" not in r.manifest


def test_r02_phase_a_before_the_boundary_is_unchanged(tmp_path):
    r = _slate(tmp_path, {"t": T_ROUND})
    assert r.ok and r.run.version_numbers() == [1] and "lock_stops" not in r.manifest
    assert not any("edit stop" in x for x in r.messages)


def test_r02_showdown_phase_a_a_started_game_publishes_nothing(tmp_path, monkeypatch):
    box = {"t": T_ROUND}
    _jump_after_build_bank(monkeypatch, box, T_STARTED)
    r = _slate(tmp_path, box, fixture="showdown2")
    assert not r.ok and r.run.version_numbers() == [] and not _public(tmp_path, r).exists()


@JUMPS
def test_r02_provisional_publish_past_the_boundary_keeps_v1(tmp_path, monkeypatch, jump):
    box = {"t": T_ROUND}
    _jump_around_publish(monkeypatch, box, "P", jump, "before")
    r = _slate(tmp_path, box, baseline_only=False, scenario=False)
    assert r.ok and r.run.version_numbers() == [1] and r.run.current_version() == 1
    assert sha256(_public(tmp_path, r).read_bytes()) == sha256(r.run.version_file(1).read_bytes())
    assert r.manifest["lock_stops"][0]["pass"] == "phase P publish"
    assert any("phase P not published" in x and "v1 stays current" in x for x in r.messages)


@JUMPS
def test_r02_scenario_publish_past_the_boundary_keeps_the_provisional_version(tmp_path, monkeypatch, jump):
    box = {"t": T_ROUND}
    _jump_around_publish(monkeypatch, box, "S", jump, "before")
    r = _slate(tmp_path, box, baseline_only=False, scenario=True, scenario_n=SMALL)
    assert r.run.version_numbers() == [1, 2] and r.run.current_version() == 2, r.manifest["failed"]
    assert sha256(_public(tmp_path, r).read_bytes()) == sha256(r.run.version_file(2).read_bytes())
    assert r.manifest["lock_stops"][-1]["pass"] == "phase S publish"
    assert any("phase S not published" in x and "v2 stays current" in x for x in r.messages)


def test_r02_optional_passes_do_not_start_once_a_game_is_inside_the_edit_stop(tmp_path, monkeypatch):
    box = {"t": T_ROUND}
    _jump_around_publish(monkeypatch, box, "A", T_EDIT_STOP, "after")  # v1 publishes in time, then the clock passes
    monkeypatch.setattr(run_mod, "_provisional_pass", lambda *a, **k: pytest.fail("the provisional pass must not start"))
    r = _slate(tmp_path, box, baseline_only=False, scenario=True, scenario_n=SMALL)
    assert r.ok and r.run.version_numbers() == [1]
    assert r.manifest["lock_stops"][0]["pass"] == "provisional and scenario passes"
    assert any("provisional and scenario passes skipped" in x and "v1 stays current" in x for x in r.messages)


def test_r02_the_scenario_pass_does_not_start_after_the_provisional_pass_crossed_the_boundary(tmp_path, monkeypatch):
    from nhl_dfs.build import scenario_pass

    box = {"t": T_ROUND}
    _jump_around_publish(monkeypatch, box, "P", T_STARTED, "after")  # the provisional version publishes in time
    monkeypatch.setattr(scenario_pass, "run_scenario_pass", lambda *a, **k: pytest.fail("the scenario pass must not start"))
    r = _slate(tmp_path, box, baseline_only=False, scenario=True, scenario_n=SMALL)
    assert r.run.version_numbers() == [1, 2]
    assert r.manifest["lock_stops"][-1]["pass"] == "scenario pass"


def test_r02_optional_work_keeps_late_swaps_margin_before_the_edit_stop(tmp_path):
    r = _slate(tmp_path, {"t": T_MARGIN}, baseline_only=False, scenario=True, scenario_n=SMALL)
    assert r.ok and r.run.version_numbers() == [1]  # T-5:20 leaves 20 s of the 25 s the objective needs
    assert any("optional work stop" in x for x in r.messages)


# the Phase B route and the multi-game diff: _export_and_publish with a crafted assignment on a published v1 ---------

def _one_cell_change(run, teams):
    """A legal one-cell change of the current version in which the player removed and the player added are both on
    `teams`: (lineups by entry, entry id, slot, removed role id, added role id)."""
    pool = read_salary(run.inputs / "DKSalaries.csv")
    cur = read_entries(run.version_file(run.current_version()))
    lineups = {e.entry_id: _canonical(cur, e) for e in cur.entries}
    for eid, lu in lineups.items():
        for k, rid in enumerate(lu):
            if pool.by_role_id[rid].team not in teams:
                continue
            for cand in pool.rows:
                if cand.team not in teams or cand.role_id in lu or not slot_accepts(CLASSIC_SLOTS[k], cand, pool.mode):
                    continue
                new = list(lu)
                new[k] = cand.role_id
                if check_lineup([pool.by_role_id[r] for r in new], pool.mode).ok:
                    return {**lineups, eid: new}, eid, k, rid, cand.role_id
    raise AssertionError(f"no legal one-cell change within {sorted(teams)}")


def _publish_phase_b(run, outs, sid, lineups, at):
    entries = read_entries(run.inputs / "DKEntries.csv")
    pool = read_salary(run.inputs / "DKSalaries.csv")
    a = Assignment(by_entry={e: tuple(lu) for e, lu in lineups.items()}, exposures={}, person_exposures={},
                   captain_exposures={}, overlap_max=0)
    m, msgs = {"failed": [], "versions": []}, []
    public = outs / sid / "DKEntries.csv"
    rec = run_mod._export_and_publish(run, entries, pool, a, sid, outs, m, msgs, phase="B", expect=sha256(public.read_bytes()),
                                      clock=lambda: at, runtime=load_runtime_config())
    return rec, m, msgs


def test_r02_a_changed_started_game_cell_is_refused_but_started_cells_left_alone_are_not_banned(baseline, tmp_path):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    before = _state(run, outs, sid)
    lineups, eid, k, old, new = _one_cell_change(run, EARLY_GAME)
    rec, m, msgs = _publish_phase_b(run, outs, sid, lineups, T_STARTED)  # AAA@BBB has started; the change is in AAA@BBB
    assert rec is None and _state(run, outs, sid) == before
    assert m["lock_stops"][0]["pass"] == "phase B publish" and any("v1 stays current" in x for x in msgs)
    assert any("1 cell(s) of a started or edit-stop game" in x for x in msgs)  # one cell, though it loses and gains a player
    # the same moment, a change confined to the two later games: AAA@BBB's cells ride along unchanged and it publishes
    lineups, eid, k, old, new = _one_cell_change(run, OPEN_GAMES)
    rec, m, msgs = _publish_phase_b(run, outs, sid, lineups, T_STARTED)
    assert rec is not None and rec["version"] == 2, msgs
    v1, v2 = (read_entries(run.version_file(n)) for n in (1, 2))
    changed = [(a.entry_id, c) for a, b in zip(v1.entries, v2.entries) for c, (x, y) in enumerate(zip(a.cells, b.cells)) if x != y]
    assert len(changed) == 1 and changed[0][0] == eid
    assert sha256(outs.joinpath(sid, "DKEntries.csv").read_bytes()) == sha256(run.version_file(2).read_bytes())


def test_r02_inside_the_edit_stop_a_predecessor_pins_its_cells_but_a_first_publish_does_not(baseline, tmp_path):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    lineups, eid, k, old, new = _one_cell_change(run, EARLY_GAME)
    rec, m, msgs = _publish_phase_b(run, outs, sid, lineups, T_EDIT_STOP)
    assert rec is None and run.version_numbers() == [1] and "lock boundary was crossed while phase B ran" in msgs[-1]
    rec, m, msgs = _publish_phase_b(run, outs, sid, lineups, T_ROUND)  # control: before the edit stop the same file publishes
    assert rec is not None and run.version_numbers() == [1, 2]


# the guard's own diff, on files crafted without the referee ------------------------------------------------------

def _crafted(run, tmp_path, changes):
    pool = read_salary(run.inputs / "DKSalaries.csv")
    cur = read_entries(run.version_file(run.current_version()))
    out = tmp_path / "proposed.csv"
    splice_cells(cur, pool, changes, out)
    return cur, read_entries(out), pool


def test_publish_crossings_names_changed_cells_of_started_and_edit_stop_games_only(baseline, tmp_path):  # noqa: F811
    run, runs, outs, sid = _copy(baseline, tmp_path)
    pool = read_salary(run.inputs / "DKSalaries.csv")
    cur = read_entries(run.version_file(1))
    lineups = {e.entry_id: _canonical(cur, e) for e in cur.entries}
    eid = cur.entries[0].entry_id
    held = lineups[eid]
    early = next(k for k, r in enumerate(held) if pool.by_role_id[r].team in EARLY_GAME)
    later = next(k for k, r in enumerate(held) if pool.by_role_id[r].team in OPEN_GAMES)
    early_rid = held[early]
    open_rid = next(r.role_id for r in pool.rows if r.team in OPEN_GAMES and r.role_id not in held)
    buf = int(load_runtime_config()["edit_stop_buffer_s"])

    ref, prop, pool = _crafted(run, tmp_path, {eid: {later: open_rid}})  # a change in an open game only
    for at in (T_ROUND, T_EDIT_STOP, T_STARTED):
        assert not locks_mod.publish_crossings(ref, prop, pool, at, buf), at
    ref, prop, pool = _crafted(run, tmp_path, {})  # no change at all, started cells and all
    assert not locks_mod.publish_crossings(ref, prop, pool, T_STARTED, buf)

    ref, prop, pool = _crafted(run, tmp_path, {eid: {early: open_rid}})  # takes a player out of AAA@BBB
    assert not locks_mod.publish_crossings(ref, prop, pool, T_ROUND, buf)
    soon = locks_mod.publish_crossings(ref, prop, pool, T_EDIT_STOP, buf)
    assert soon.edit_stop and not soon.started and "replaces" in soon.edit_stop[0] and f"entry {eid} slot {early}" in soon.edit_stop[0]
    gone = locks_mod.publish_crossings(ref, prop, pool, T_STARTED, buf)
    assert gone.started and not gone.edit_stop and bool(gone)

    added = next(r.role_id for r in pool.rows if r.team in EARLY_GAME and r.role_id not in held)
    ref, prop, pool = _crafted(run, tmp_path, {eid: {later: added}})  # puts a player of AAA@BBB into an open game's cell
    adds = locks_mod.publish_crossings(ref, prop, pool, T_STARTED, buf)
    assert adds.started and "adds" in adds.started[0] and early_rid in held
