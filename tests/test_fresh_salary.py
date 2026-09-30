"""Backlog B1, B27: late swap and refresh accept a re-downloaded DKSalaries.csv of the same slate when it differs
from the run's only by ADDED rows (DraftKings adds a late player); a changed or removed original row is refused.
Lock semantics stay C2c's."""
import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import TESTS
from nhl_dfs.build import late_swap, refresh
from nhl_dfs.build.run import run_slate
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import added_rows_diff, read_salary
from nhl_dfs.referee.check_file import check_file

pytestmark = [pytest.mark.c2c, pytest.mark.c9]

LS = TESTS / "fixtures" / "late_swap" / "classic"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)  # AAA@BBB starts; CCC@DDD 00:00Z, EEE@FFF 02:00Z
NEW_ID = "81000099"
NEW_ROW = ["C", f"Zulu C9 ({NEW_ID})", "Zulu C9", NEW_ID, "C/UTIL", "2500", "EEE@FFF 10/15/2026 10:00PM ET", "EEE",
           "60.0", "", ""]


def _rows(path=LS / "DKSalaries.csv"):
    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def _write(rows, dst):
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    dst.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    return dst


def fresh_salary(dst, *, add=True, out=("Echo C1",), edit=None, drop=None):
    """A re-download of the fixture's salary file: one added row, Status OUT for `out`, optional edits."""
    rows = _rows()
    for rec in rows[1:]:
        if rec and rec[2] in out:
            rec[9] = "OUT"
        if edit and rec and rec[2] == edit[0]:
            rec[edit[1]] = edit[2]
    if drop:
        rows = [r for r in rows if not (r and r[2] == drop)]
    if add:
        rows.append(list(NEW_ROW))
    return _write(rows, dst)


def with_embedded(src, dst, ids):
    """A copy of an entries export with DK's embedded player list (the IDs given) to the right of the entries."""
    rows = list(csv.reader(io.StringIO(src.read_bytes().decode("utf-8-sig"), newline="")))
    base = rows[0].index("Instructions")
    width = base + 4
    rows = [r + [""] * (width - len(r)) for r in rows]
    rows.append([""] * base + ["Position", "Name + ID", "Name", "ID"])
    rows += [[""] * base + ["C", f"x ({i})", "x", i] for i in sorted(ids)]
    return _write(rows, dst)


def base(tmp_path):
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def swap(tmp_path, run_id, current, salary, as_of):
    return late_swap.run(run_id, current, offline=True, fast=True, runs_root=tmp_path / "runs",
                         outputs_root=tmp_path / "outputs", salary_path=salary, as_of=as_of)


def cells(path):
    return {e.entry_id: e.cells for e in read_entries(path).entries}


def test_the_diff_accepts_added_rows_only_and_names_what_changed(tmp_path):
    old = read_salary(LS / "DKSalaries.csv")
    d = added_rows_diff(old, read_salary(fresh_salary(tmp_path / "a.csv")))
    assert d.ok and [r.role_id for r in d.added] == [NEW_ID] and not d.removed and not d.changed
    d = added_rows_diff(old, read_salary(fresh_salary(tmp_path / "b.csv", edit=("Alpha C2", 5, "5400"))))
    assert not d.ok and "Alpha C2 (81000002) salary: 5200 -> 5400" in d.refusal
    d = added_rows_diff(old, read_salary(fresh_salary(tmp_path / "c.csv", drop="Alpha C2")))
    assert not d.ok and "missing" in d.refusal and "Alpha C2" in d.refusal
    d = added_rows_diff(old, read_salary(fresh_salary(tmp_path / "d.csv", edit=("Alpha C2", 6, "AAA@BBB 10/15/2026 07:30PM ET"))))
    assert not d.ok and "game_info" in d.refusal  # a moved start time changes an original row: refused, named
    assert added_rows_diff(old, old).ok  # the same file: nothing added, nothing refused


def test_late_swap_accepts_an_added_player_reports_him_and_can_place_him_in_a_free_cell(tmp_path):
    b = base(tmp_path)
    cur = LS / "DKEntries.current.csv"
    fresh = fresh_salary(tmp_path / "DKSalaries (1).csv")
    now = G1 + timedelta(minutes=10)
    s = swap(tmp_path, b.run.run_id, cur, fresh, now)
    assert s.statuses["FILE_VALID"] == "TRUE", s.messages
    assert s.manifest["salary_added_ids"] == [NEW_ID]
    assert s.manifest["salary_diff"]["added"][0]["name"] == "Zulu C9"
    assert any(m.startswith("SALARY_DIFF: DraftKings added 1 row(s)") and "Zulu C9" in m for m in s.messages)
    out = s.run.version_file(1)
    placed = {cell_role_id(c) for c in cells(out)["7100000001"]}
    assert NEW_ID in placed  # Echo C1 is OUT; the added $2,500 C (APPG 60) is the repair
    pool = read_salary(fresh)
    started = {r.role_id for r in pool.rows if r.team in ("AAA", "BBB")}
    before = cells(cur)
    for eid, row in before.items():  # pinned (started) cells byte-identical, as in C2c
        for k, text in enumerate(row):
            if cell_role_id(text) in started:
                assert cells(out)[eid][k] == text
    for c in s.manifest["changed_cells"]:
        assert pool.by_role_id[c["to"]].team not in ("AAA", "BBB")  # no started player added
    orig = read_salary(LS / "DKSalaries.csv")
    assert all(pool.by_role_id[r.role_id].salary == r.salary for r in orig.rows)  # every original ID and salary kept
    assert check_file(out, s.run.inputs / "DKSalaries.csv", s.run.inputs / "DKEntries.csv").ok


def test_a_changed_or_removed_original_row_is_refused_and_says_why(tmp_path):
    b = base(tmp_path)
    cur = LS / "DKEntries.current.csv"
    for name, kw, why in (("changed", {"edit": ("Alpha C2", 5, "5400")}, "salary: 5200 -> 5400"),
                          ("removed", {"drop": "Alpha C2"}, "missing from the fresh file")):
        s = swap(tmp_path, b.run.run_id, cur, fresh_salary(tmp_path / f"{name}.csv", **kw), G1 + timedelta(minutes=10))
        assert s.statuses["DELIVERY_STATUS"] == "FAILED" and s.run.version_numbers() == []
        assert any("salary file refused" in m and why in m for m in s.messages), s.messages


def test_nothing_to_repair_with_an_added_only_file_returns_the_input_bytes(tmp_path):
    b = base(tmp_path)
    cur = b.run.version_file(1)
    s = swap(tmp_path, b.run.run_id, cur, fresh_salary(tmp_path / "f.csv", out=()), BEFORE)
    assert s.statuses["FILE_VALID"] == "TRUE" and s.manifest["changed_cells"] == []
    assert s.run.version_file(1).read_bytes() == cur.read_bytes()


def test_an_export_that_lists_players_the_salary_file_lacks_is_refused_up_front(tmp_path):
    b = base(tmp_path)
    ids = set(read_salary(LS / "DKSalaries.csv").by_role_id) | {NEW_ID}
    cur = with_embedded(LS / "DKEntries.current.csv", tmp_path / "cur.csv", ids)
    s = swap(tmp_path, b.run.run_id, cur, None, G1 + timedelta(minutes=10))
    assert s.statuses["DELIVERY_STATUS"] == "FAILED" and any("re-download DKSalaries.csv" in m for m in s.messages)
    s2 = swap(tmp_path, b.run.run_id, cur, fresh_salary(tmp_path / "g.csv"), G1 + timedelta(minutes=10))
    assert s2.statuses["FILE_VALID"] == "TRUE", s2.messages


def test_the_referee_accepts_an_older_export_only_for_declared_added_ids(tmp_path):
    b = base(tmp_path)
    fresh = fresh_salary(tmp_path / "h.csv", out=())
    old_ids = set(read_salary(LS / "DKSalaries.csv").by_role_id)
    cur = with_embedded(b.run.version_file(1), tmp_path / "cur.csv", old_ids)  # exported before DK added Zulu C9
    assert not check_file(cur, fresh, cur).ok  # undeclared: a draft-group mismatch, as before
    rep = check_file(cur, fresh, cur, added_ids=frozenset({NEW_ID}))
    assert rep.ok and any("added by DraftKings" in n for n in rep.notes)
    short = with_embedded(b.run.version_file(1), tmp_path / "x.csv", old_ids - {"81000002"})
    assert not check_file(short, fresh, short, added_ids=frozenset({NEW_ID})).ok  # beyond the declared rows: fails
    s = swap(tmp_path, b.run.run_id, cur, fresh, BEFORE)
    assert s.statuses["FILE_VALID"] == "TRUE", s.messages
    from nhl_dfs.cli import verify_run

    ok, lines = verify_run(tmp_path / "runs", s.run.run_id)
    assert ok, lines


def test_refresh_accepts_a_fresh_file_and_its_child_stays_swappable_on_the_same_slate(tmp_path):
    b = base(tmp_path)
    fresh = fresh_salary(tmp_path / "DKSalaries (1).csv")
    f = refresh.run(b.run.run_id, offline=True, runs_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                    salary_path=fresh, as_of=G1 + timedelta(minutes=10))
    assert f.statuses["FILE_VALID"] == "TRUE", f.messages
    assert any("Zulu C9" in m for m in f.messages) and f.slate_id == b.slate_id
    # the scheduled refresh (no --salary) from the child: accepted, same slate, same outputs folder
    f2 = refresh.run(f.run.run_id, offline=True, runs_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                     as_of=G1 + timedelta(minutes=20))
    assert f2.statuses["FILE_VALID"] == "TRUE", f2.messages
    assert f2.slate_id == b.slate_id and f2.manifest["salary_added_ids"] == [NEW_ID]
    assert Path(f2.manifest["public_path"]).parent.name == b.slate_id  # the same slate folder (rehearsal: in the run)
    s = swap(tmp_path, f2.run.run_id, f2.run.version_file(1), None, G1 + timedelta(minutes=30))
    assert s.statuses["FILE_VALID"] == "TRUE", s.messages


def test_the_cli_takes_the_fresh_salary_file_as_a_positional(tmp_path, capsys):
    from nhl_dfs.cli import main

    b = base(tmp_path)
    fresh = fresh_salary(tmp_path / "DKSalaries (1).csv")
    roots = ["--runs-root", str(tmp_path / "runs"), "--outputs-root", str(tmp_path / "outputs"), "--offline",
             "--as-of", "2026-10-15T23:10:00Z"]
    assert main(["late-swap", b.run.run_id, str(LS / "DKEntries.current.csv"), str(fresh), "--fast", *roots]) == 0
    assert "SALARY_DIFF: DraftKings added 1 row(s)" in capsys.readouterr().out
    assert main(["refresh", b.run.run_id, str(fresh), *roots]) == 0
    assert "Zulu C9" in capsys.readouterr().out
    assert main(["refresh", b.run.run_id, str(fresh), "extra", *roots]) == 2


def test_under_the_scenario_objective_the_added_players_game_is_resimulated_and_he_has_draws(tmp_path):
    from conftest import mini_pair
    from nhl_dfs.build import scenario_cache as sc

    sal, ent = mini_pair("classic")
    r = run_slate(sal, ent, offline=True, baseline_only=False, scenario=True,
                  scenario_n={"design": 300, "selection": 800, "referee": 800, "field_target": 400},
                  out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                  clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert r.ok, r.manifest["failed"]
    rows = _rows(sal)
    tmpl = next(x for x in rows[1:] if x and x[7] == "EDM" and x[0] == "C")
    add = list(tmpl)
    add[1], add[2], add[3], add[5], add[8] = f"Zulu Late ({NEW_ID})", "Zulu Late", NEW_ID, "2500", "12.0"
    add[9:] = [""] * (len(add) - 9)
    fresh = _write(rows + [add], tmp_path / "DKSalaries (1).csv")
    f = refresh.run(r.run.run_id, offline=True, runs_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                    salary_path=fresh, as_of=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    assert f.ok, f.messages
    o = f.manifest["objective"]
    assert o["kind"] == "scenario" and "VAN@EDM" in o["scenario"]["games_resimulated"]
    got = sc.load(f.run.path, f.run.run_id)
    pk = read_salary(fresh).by_role_id[NEW_ID].person_key
    j = got.person_keys.index(pk)
    assert got.base("selection", got.n("selection"))[:, j].any()  # the added player is priced, not a column of zeros
    assert got.flags("selection", got.n("selection"))[:, j, 0].any()
