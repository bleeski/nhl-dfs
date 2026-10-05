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
from nhl_dfs.build.late_swap import splice_cells
from nhl_dfs.build.run import run_slate
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import added_rows_diff, read_salary, with_started_rows
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
