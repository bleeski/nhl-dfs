"""C15: started slates (B42 parts 2 and 3).

DraftKings replaces Game Info with "In-Progress" once a game has started (part 1, tests/test_started_game_intake.py:
those rows leave the selectable pool). This file covers what follows from it: a cell that holds a started-game player
is a pinned cell even though its row has no start time and is not in the pool (late swap keeps it and fills the
rest); a run that starts after the first game builds the open games; the slate id does not move when rows leave the
pool. The fixture is the late-swap Classic slate: AAA@BBB starts 23:00Z, CCC@DDD 00:00Z, EEE@FFF 02:00Z. The
In-Progress file is derived from it the way test_started_game_intake does: the real DKSalaries_127 of 2026-10-02 is
not on every machine, and the derived file carries the same marker on the same kind of rows.
"""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TESTS
from nhl_dfs.build import late_swap
from nhl_dfs.build import locks as locks_mod
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.late_swap import splice_cells
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, slot_accepts
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import added_rows_diff, read_salary, with_started_rows
from nhl_dfs.referee.check_file import check_file
from test_started_game_intake import _with_started_game

pytestmark = pytest.mark.c15

LS = TESTS / "fixtures" / "late_swap" / "classic"
EARLY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # every game is still ahead
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
MARKERS = ["In-Progress", "In Progress"]
STARTED_TEAMS = {"AAA", "BBB"}


def _in_progress(tmp_path, marker="In-Progress"):
    path, n = _with_started_game(tmp_path, marker)
    assert n > 0
    return path


def _started_ids():
    return {r.role_id for r in read_salary(LS / "DKSalaries.csv").rows if r.team in STARTED_TEAMS}


def _cells(path) -> dict[str, tuple[str, ...]]:
    return {e.entry_id: e.cells for e in read_entries(path).entries}


def _parent(tmp_path):
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: EARLY)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


# -- part 2: pins ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("marker", MARKERS)
def test_a_started_game_occupant_is_pinned_by_the_marker_alone(tmp_path, marker):
    """The row has no start time and has left the pool; the clock is hours before the game. The cell is still LOCKED
    "started", the entry is still readable, and the player cannot be added anywhere."""
    pool = read_salary(_in_progress(tmp_path, marker))
    current = read_entries(LS / "DKEntries.current.csv")
    started = _started_ids()
    assert not (started & set(pool.by_role_id))  # part 1: out of the selectable pool
    ls = locks_mod.compute(current, pool, None, EARLY, 300)
    pinned = {(eid, k): c for (eid, k), c in ls.cells.items() if c.role_id in started}
    assert pinned, "the fixture's lineups hold players of the started game"
    for c in pinned.values():
        assert c.lock.value == "LOCKED" and c.reason.startswith("started") and c.start_utc is None
    assert not ls.unreadable_entries
    assert started <= ls.started_role_ids and started <= ls.not_addable
    assert all(not ls.addable(rid) for rid in started)
    assert {c.lock.value for key, c in ls.cells.items() if key not in pinned} <= {"OPEN"}
    assert ls.started_games == {"AAA (in progress)", "BBB (in progress)"}  # teams only: DK's marker names no opponent


def test_the_pins_equal_the_clock_pins_of_the_pre_start_file(tmp_path):
    """The same cells are pinned whether the marker or the clock says the game started."""
    current = read_entries(LS / "DKEntries.current.csv")
    by_marker = locks_mod.compute(current, read_salary(_in_progress(tmp_path)), None, EARLY, 300)
    by_clock = locks_mod.compute(current, read_salary(LS / "DKSalaries.csv"), None, G1 + timedelta(minutes=10), 300)
    assert {k for k, c in by_marker.cells.items() if c.pinned} == {k for k, c in by_clock.cells.items() if c.pinned}


@pytest.mark.parametrize("now", [EARLY, G1 + timedelta(minutes=10)], ids=["before_the_start", "after_the_start"])
def test_late_swap_on_a_post_lock_export_keeps_the_started_cells_and_fills_the_rest(tmp_path, now):
    """Ben re-downloads DKSalaries.csv after the game started (In-Progress rows) and passes it as the third file. The
    cells of the started game stay byte for byte; the blank and the repairable entries are filled from the open games
    and match what the pre-start file gives at the same lock state."""
    salary = _in_progress(tmp_path)
    b = _parent(tmp_path)
    cur = LS / "DKEntries.current.csv"
    r = late_swap.run(b.run.run_id, cur, runs_root=tmp_path / "runs", as_of=now, offline=True, salary_path=salary)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:4]
    assert not any("not in the salary pool" in m or "refused" in m for m in r.messages)
    started = _started_ids()
    before, after = _cells(cur), _cells(r.run.version_file(1))
    held = 0
    for eid, row in before.items():
        for k, text in enumerate(row):
            if cell_role_id(text) in started:  # pinned: the exact bytes stay
                assert after[eid][k] == text
                held += 1
    assert held > 0
    pool = read_salary(LS / "DKSalaries.csv")
    changed = r.manifest["changed_cells"]
    assert {c["entry_id"] for c in changed} == {"7100000001", "7100000005"}  # the OUT player's entry, the blank one
    for c in changed:
        assert pool.by_role_id[c["to"]].team not in STARTED_TEAMS  # a started-game player is never added
    assert r.manifest["locks"]["counts"]["LOCKED"] == held
    # the same answer as the pre-start file at the same lock state
    ref = late_swap.run(_parent(tmp_path / "ref").run.run_id, cur, runs_root=tmp_path / "ref" / "runs",
                        as_of=G1 + timedelta(minutes=10), offline=True)
    assert [(c["entry_id"], c["slot"], c["to"]) for c in changed] == \
        [(c["entry_id"], c["slot"], c["to"]) for c in ref.manifest["changed_cells"]]


def test_a_fresh_in_progress_file_is_a_re_download_not_a_different_slate(tmp_path):
    old = read_salary(LS / "DKSalaries.csv")
    new = read_salary(_in_progress(tmp_path))
    diff = added_rows_diff(old, new)
    assert diff.ok, diff.refusal
    assert diff.removed == [] and diff.changed == [] and diff.added == []


def test_a_started_row_whose_salary_changed_is_still_refused(tmp_path):
    path = _in_progress(tmp_path)
    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))
    sal, gi = rows[0].index("Salary"), rows[0].index("Game Info")
    victim = next(r for r in rows[1:] if r[gi] == "In-Progress")
    victim[sal] = str(int(victim[sal]) + 100)
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    path.write_bytes(out.getvalue().encode("utf-8"))
    diff = added_rows_diff(read_salary(LS / "DKSalaries.csv"), read_salary(path))
    assert not diff.ok and "salary" in diff.refusal


def test_the_publish_guard_sees_a_changed_started_cell_though_its_row_left_the_pool(tmp_path):
    """C14's guard skipped an ID with no selectable row, so a started-game cell changed by a later version went
    unseen on a post-start file. Changing it, or adding such a player, is a crossing."""
    pool = read_salary(_in_progress(tmp_path))
    current = read_entries(LS / "DKEntries.current.csv")
    started = _started_ids()
    (eid, col, rid) = next((e.entry_id, c, cell_role_id(t)) for e in current.entries for c, t in enumerate(e.cells)
                           if cell_role_id(t) in started)
    full = read_salary(LS / "DKSalaries.csv")
    other = next(r for r in pool.rows if r.position == full.by_role_id[rid].position and r.role_id not in
                 {cell_role_id(t) for e in current.entries if e.entry_id == eid for t in e.cells})
    from nhl_dfs.intake.entries import template_permutation

    k = template_permutation(current.roster_labels, current.mode)[col]
    out = tmp_path / "proposed.csv"
    splice_cells(current, with_started_rows(pool), {eid: {k: other.role_id}}, out)
    found = locks_mod.publish_crossings(current, read_entries(out), pool, EARLY, 300)
    assert found.started and any(full.by_role_id[rid].name in line for line in found.started)
    same = locks_mod.publish_crossings(current, current, pool, EARLY, 300)
    assert not same  # a started cell left as it was is never a crossing


def test_the_entries_file_listing_in_progress_in_its_player_list_reads(tmp_path):
    """The entries file's embedded list says "In Progress" for a started game; the reader takes its IDs and nothing else."""
    mini = TESTS / "fixtures" / "mini" / "classic" / "DKEntries.csv"
    records = list(csv.reader(io.StringIO(mini.read_bytes().decode("utf-8-sig"), newline="")))
    header = next(i for i, r in enumerate(records) if "Game Info" in r)
    gcol = records[header].index("Game Info")
    for rec in records[header + 1:]:
        if len(rec) > gcol and rec[gcol]:
            rec[gcol] = "In Progress"
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(records)
    path = tmp_path / "DKEntries.csv"
    path.write_bytes(out.getvalue().encode("utf-8"))
    from nhl_dfs.referee.reader import read_entries_min

    assert read_entries(path).entries and read_entries_min(path).embedded_ids == read_entries_min(mini).embedded_ids


# -- part 3: the started-slate build --------------------------------------------------------------------------------

SD = TESTS / "fixtures" / "late_swap" / "showdown2"
AFTER_LAST = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)  # past EEE@FFF, the last game


def _mark(src, dst, game="AAA@BBB", marker="In-Progress"):
    """Copy a salary file with every row of `game` carrying DK's In-Progress marker."""
    records = list(csv.reader(io.StringIO(src.read_bytes().decode("utf-8-sig"), newline="")))
    gi = records[0].index("Game Info")
    for rec in records[1:]:
        if rec and rec[gi].startswith(game):
            rec[gi] = marker
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(records)
    dst.write_bytes(out.getvalue().encode("utf-8"))
    return dst


def _start(tmp_path, salary, entries, clock=EARLY, **kw):
    return run_slate(salary, entries, offline=True, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                     clock=lambda: clock, **kw)


def _lineups(path) -> dict[str, list[str | None]]:
    ef = read_entries(path)
    return {e.entry_id: late_swap._canonical(ef, e) for e in ef.entries}


def _team_of():
    return {r.role_id: r.team for r in read_salary(LS / "DKSalaries.csv").rows}


@pytest.mark.parametrize("marker", MARKERS)
def test_a_blank_entries_export_builds_with_no_started_game_player(tmp_path, marker):
    """The 2026-10-02 case: the game started before the file was downloaded, the entries are blank. The run builds the open
    games, names the started teams and the excluded players in stdout and RUN_NOTES, and nothing is called checked
    that the checks did not cover (the referee passed the written bytes; no pinned entry was left unrepaired)."""
    salary = _in_progress(tmp_path, marker)
    r = _start(tmp_path, salary, LS / "DKEntries.template.csv")
    assert r.statuses["FILE_VALID"] == "TRUE" and r.public_path is not None, r.messages[:4]
    teams = _team_of()
    out = _lineups(r.public_path)
    assert len(out) == 5 and all(len(lu) == 9 and None not in lu for lu in out.values())
    assert not {teams[x] for lu in out.values() for x in lu} & STARTED_TEAMS
    s = r.manifest["started_slate"]
    assert s["started_teams"] == ["AAA", "BBB"] and s["open_games"] == ["CCC@DDD", "EEE@FFF"] and s["pinned_cells"] == 0
    assert set(s["excluded_players"]) == STARTED_TEAMS and sum(map(len, s["excluded_players"].values())) == s["excluded_rows"]
    said = " ".join(r.messages)
    assert "STARTED SLATE" in said and "AAA" in said and "BBB" in said and "Alpha C1" in said
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    assert "Started slate" in notes and "Alpha C1" in notes and "CCC@DDD" in notes
    assert r.statuses["DELIVERY_STATUS"] == "CHECKED"  # leaving a started game out is not a degradation
    assert r.run.version_numbers() == [1] and "lock_stops" not in r.manifest
    assert check_file(r.public_path, r.run.inputs / "DKSalaries.csv", r.run.inputs / "DKEntries.csv").ok


def test_a_run_past_the_first_start_by_the_clock_builds_the_open_games(tmp_path):
    """A pre-start file (real Game Info) read after the first game began is treated exactly as the marker: the old run
    refused here (flag 15 now has the default in force)."""
    r = _start(tmp_path, LS / "DKSalaries.csv", LS / "DKEntries.template.csv", clock=G1 + timedelta(minutes=10))
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:4]
    teams = _team_of()
    assert not {teams[x] for lu in _lineups(r.public_path).values() for x in lu} & STARTED_TEAMS
    s = r.manifest["started_slate"]
    assert s["started_teams"] == ["AAA", "BBB"] and "Game Info start time" in s["how"] and "marker" not in s["how"]
    assert r.manifest["slate_id"].startswith("classic-20261015-")


def test_a_slate_with_no_open_game_left_is_refused_and_names_late_swap(tmp_path):
    r = _start(tmp_path, LS / "DKSalaries.csv", LS / "DKEntries.template.csv", clock=AFTER_LAST)
    assert r.statuses["FILE_VALID"] == "FALSE" and r.statuses["DELIVERY_STATUS"] == "FAILED"
    assert r.public_path is None and r.run.version_numbers() == []
    assert any("late swap" in m and "every game" in m for m in r.messages)


def test_entries_that_hold_started_players_keep_those_cells_and_are_rebuilt_around_them(tmp_path):
    """Pins (B42 part 2) in the initial run: the entry's started-game cells stay in their slots, its other cells come
    from the open games, and the guard in `_export_and_publish` sees no started-game change it did not pin."""
    salary = _in_progress(tmp_path)
    cur = LS / "DKEntries.current.csv"
    r = _start(tmp_path, salary, cur)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.public_path is not None, r.messages[:4]
    teams, started = _team_of(), _started_ids()
    before, after = _lineups(cur), _lineups(r.public_path)
    pinned = 0
    for eid, lu in before.items():
        for k, rid in enumerate(lu):
            if rid in started:
                assert after[eid][k] == rid
                pinned += 1
        assert [x for x in after[eid] if x in started] == [x for x in lu if x in started]  # none added, none dropped
        assert all(teams[x] not in STARTED_TEAMS for k, x in enumerate(after[eid]) if lu[k] not in started)
    assert pinned > 0
    s = r.manifest["started_slate"]
    assert s["pinned_cells"] == pinned and s["pinned_entries"] == sorted(e for e, lu in before.items() if set(lu) & started)
    assert any(after[e][k] != before[e][k] for e in after for k in range(9) if before[e][k] not in started)  # rebuilt
    assert r.statuses["DELIVERY_STATUS"] == "CHECKED" and "lock_stops" not in r.manifest
    assert "Pinned cells" in (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")


def _tamper_a_pinned_cell(monkeypatch, cur):
    """Referee off and `assign` made to move one pinned started-game cell to an open-game player: only the lock guard
    stands between that file and the public path."""
    from dataclasses import replace

    real_assign, real_check = run_mod.assign, run_mod.check_file
    started = _started_ids()
    eid = next(e for e, lu in _lineups(cur).items() if set(lu) & started)
    k = next(i for i, x in enumerate(_lineups(cur)[eid]) if x in started)

    def tamper(candidates, entries, pool, *a, **kw):
        out = real_assign(candidates, entries, pool, *a, **kw)
        swap = next(r.role_id for r in pool.rows if r.role_id not in started and r.role_id not in out.by_entry[eid])
        lu = list(out.by_entry[eid])
        lu[k] = swap
        out.by_entry[eid] = tuple(lu)
        return out

    monkeypatch.setattr(run_mod, "assign", tamper)
    monkeypatch.setattr(run_mod, "check_file", lambda *a, **kw: replace(real_check(*a, **kw), ok=True, reasons=[]))
    return eid, k


def test_the_started_slate_build_reaches_the_publish_guard_and_the_guard_still_stops_a_moved_pin(tmp_path, monkeypatch):
    """Negative control for flag 20: the guard is unchanged and not bypassed. A build that moves a pinned started-game
    cell publishes nothing (the Phase A rule with no predecessor), and says why."""
    salary = _in_progress(tmp_path)
    cur = LS / "DKEntries.current.csv"
    _tamper_a_pinned_cell(monkeypatch, cur)
    r = _start(tmp_path, salary, cur)
    assert r.public_path is None and r.run.version_numbers() == []
    assert r.statuses["FILE_VALID"] == "FALSE" and r.statuses["DELIVERY_STATUS"] == "FAILED"
    stop = r.manifest["lock_stops"][0]
    assert "started game" in stop["reason"] and stop["pass"] == "phase A publish"
    assert not (tmp_path / "outputs").exists() or not list((tmp_path / "outputs").rglob("DKEntries.csv"))


def test_the_real_started_slate_build_changes_no_started_game_cell_the_guard_would_stop(tmp_path):
    """Control for the test above, with nothing patched: the same inputs publish, and against the uploaded file no
    started-game cell differs (publish_crossings reports nothing)."""
    salary = _in_progress(tmp_path)
    cur = LS / "DKEntries.current.csv"
    r = _start(tmp_path, salary, cur)
    assert r.public_path is not None
    pool = read_salary(r.run.inputs / "DKSalaries.csv")
    found = locks_mod.publish_crossings(read_entries(cur), read_entries(r.public_path), pool, EARLY, 300)
    assert not found.started and not found.edit_stop


def test_an_entry_the_pins_leave_unsolvable_keeps_its_cells_and_the_file_is_not_called_checked(tmp_path, monkeypatch):
    salary = _in_progress(tmp_path)
    cur = LS / "DKEntries.current.csv"
    monkeypatch.setattr(late_swap, "_solve_entry", lambda *a, **k: (None, "milp", (), "forced"))
    r = _start(tmp_path, salary, cur)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    s = r.manifest["started_slate"]
    assert s["unrepaired_entries"] == s["pinned_entries"] and s["pinned_entries"]
    before, after = _lineups(cur), _lineups(r.public_path)
    assert all(after[e] == before[e] for e in s["unrepaired_entries"])
    assert any("current cells are kept" in m for m in r.messages)


def test_an_entry_with_no_legal_completion_stops_the_run(tmp_path):
    """Nine started-game cells are fewer than the three teams DraftKings requires and cannot be completed: nothing is
    published rather than a file the referee would have to reject."""
    pool = read_salary(LS / "DKSalaries.csv")
    taken: list[str] = []
    for slot in CLASSIC_SLOTS:
        pick = next(r for r in pool.rows if r.team in STARTED_TEAMS and r.role_id not in taken and slot_accepts(slot, r, pool.mode))
        taken.append(pick.role_id)
    text = (LS / "DKEntries.current.csv").read_bytes().decode("utf-8-sig")
    lines = text.split("\r\n")
    cells = [f"{pool.by_role_id[x].name} ({x})" for x in taken]
    head, rest = lines[5].split(",", 4)[:4], None
    lines[5] = ",".join(head + cells + ["", ""])
    path = tmp_path / "DKEntries.csv"
    path.write_bytes(("\r\n".join(lines)).encode("utf-8-sig"))
    r = _start(tmp_path, _in_progress(tmp_path), path)
    assert r.public_path is None and r.statuses["FILE_VALID"] == "FALSE"
    assert any("no legal lineup keeps the started-game cell" in m for m in r.messages)


def test_a_showdown_slate_with_one_game_started_builds_the_other(tmp_path):
    salary = _mark(SD / "DKSalaries.csv", tmp_path / "DKSalaries.csv")
    r = _start(tmp_path, salary, SD / "DKEntries.template.csv")
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:4]
    teams = {x.role_id: x.team for x in read_salary(SD / "DKSalaries.csv").rows}
    assert not {teams[x] for lu in _lineups(r.public_path).values() for x in lu} & STARTED_TEAMS
    assert r.manifest["started_slate"]["open_games"] == ["CCC@DDD"]


def test_the_qa_packet_and_research_request_work_on_a_run_that_holds_started_players(tmp_path):
    """`slate` builds both after the run. They read the published file, whose pinned cells hold started-game players
    that are not in the pool: the cells resolve, and nobody from the started game is a research target or an alternative."""
    from nhl_dfs.build import packet
    from nhl_dfs.build.state import open_run

    r = _start(tmp_path, _in_progress(tmp_path), LS / "DKEntries.current.csv")
    assert r.public_path is not None
    run = open_run(tmp_path / "runs", r.run.run_id)
    started = _started_ids()
    req = packet.research_request(run, now=EARLY, runs_root=tmp_path / "runs")
    assert not {p["role_id"] for p in req["players"]} & started
    pk = packet.build(run, 1, now=EARLY, runs_root=tmp_path / "runs")
    assert pk["locks"]["counts"]["LOCKED"] == r.manifest["started_slate"]["pinned_cells"]
    assert pk["locks"]["started_games"] == ["AAA (in progress)", "BBB (in progress)"]
    assert not {a["alt"]["role_id"] for a in pk.get("alternatives", [])} & started


# -- the stable slate id --------------------------------------------------------------------------------------------

# Literal ids of the fixtures as origin/master (a4c45e7) computes them: a pool with no game in progress must keep its id,
# or every outputs/<slate>/, data/entered/<slate>.csv and settle record of an existing slate would be orphaned.
PRE_START_IDS = {
    "late_swap/classic": "classic-20261015-9bbe797c4e",
    "late_swap/showdown2": "showdown-20261015-bbde96aa67",
    "mini/classic": "classic-20260929-868f7d8a0d",
    "mini/showdown": "showdown-20260929-ff72be646a",
}


@pytest.mark.parametrize("fixture", sorted(PRE_START_IDS))
def test_a_pool_with_no_started_game_keeps_the_id_it_always_had(fixture):
    from nhl_dfs.build.run import slate_id_for

    assert slate_id_for(read_salary(TESTS / "fixtures" / fixture / "DKSalaries.csv")) == PRE_START_IDS[fixture]


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("fixture", ["late_swap/classic", "late_swap/showdown2"])
def test_the_slate_id_of_a_started_game_file_equals_the_pre_start_id(tmp_path, fixture, marker):
    from nhl_dfs.build.run import slate_id_for

    pre = read_salary(TESTS / "fixtures" / fixture / "DKSalaries.csv")
    post = read_salary(_mark(TESTS / "fixtures" / fixture / "DKSalaries.csv", tmp_path / "DKSalaries.csv", marker=marker))
    assert post.started_rows and len(post.by_role_id) < len(pre.by_role_id)  # rows really left the pool
    assert slate_id_for(post) == slate_id_for(pre) == PRE_START_IDS[fixture]


def test_the_id_follows_the_whole_draft_group_not_the_rows_left_in_the_pool(tmp_path):
    """Two different groups never share an id because of the marker: one more game started leaves a different set."""
    from nhl_dfs.build.run import slate_id_for

    one = read_salary(_mark(LS / "DKSalaries.csv", tmp_path / "one.csv"))
    other = read_salary(_mark(LS / "DKSalaries.csv", tmp_path / "other.csv", game="CCC@DDD"))
    assert slate_id_for(one) == slate_id_for(other)  # the same draft group, a different game in progress
    swapped = tmp_path / "different.csv"
    records = list(csv.reader(io.StringIO((LS / "DKSalaries.csv").read_bytes().decode("utf-8-sig"), newline="")))
    records[1][records[0].index("ID")] = "99999999"
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(records)
    swapped.write_bytes(out.getvalue().encode("utf-8"))
    assert slate_id_for(read_salary(swapped)) != slate_id_for(read_salary(LS / "DKSalaries.csv"))


def test_a_started_slate_run_publishes_under_the_pre_start_slate_id(tmp_path):
    """The file made after the game started lands in the same outputs/<slate>/ as the one made before it, so the
    entered-contest list, settle and the added-rows diff all find one slate."""
    for d in ("pre", "post", "clock"):
        (tmp_path / d).mkdir()
    before = _start(tmp_path / "pre", LS / "DKSalaries.csv", LS / "DKEntries.template.csv")
    after = _start(tmp_path / "post", _in_progress(tmp_path), LS / "DKEntries.template.csv")
    assert before.slate_id == after.slate_id == PRE_START_IDS["late_swap/classic"]
    assert after.public_path == tmp_path / "post" / "outputs" / before.slate_id / "DKEntries.csv"
    by_clock = _start(tmp_path / "clock", LS / "DKSalaries.csv", LS / "DKEntries.template.csv", clock=G1 + timedelta(minutes=10))
    assert by_clock.slate_id == before.slate_id
