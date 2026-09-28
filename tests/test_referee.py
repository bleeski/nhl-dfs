import ast
from pathlib import Path

import pytest
from conftest import REPO_ROOT, mini_pair, real_pair
from lineup_helpers import fill_assignment

from nhl_dfs import cli
from nhl_dfs.export.writer import write_entries
from nhl_dfs.intake.entries import physical_lines, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import check_file

pytestmark = pytest.mark.c0b


def _written(mode, tmp_path):
    sal, ent = mini_pair(mode)
    pool, entries = read_salary(sal), read_entries(ent)
    out_path = tmp_path / f"{mode}_out.csv"
    write_entries(entries, fill_assignment(entries, pool), pool, out_path)
    return sal, ent, out_path, pool


def _edit(path: Path, old: bytes, new: bytes) -> None:
    raw = path.read_bytes()
    assert raw.count(old) >= 1, old
    path.write_bytes(raw.replace(old, new, 1))


def _cell(pool, name, rp=None):
    row = next(r for r in pool.rows if r.name == name and (rp is None or rp in r.roster_positions))
    return f"{row.name} ({row.role_id})".encode()


def test_written_mini_files_pass(tmp_path):
    for mode in ("classic", "showdown"):
        sal, ent, out, _ = _written(mode, tmp_path)
        report = check_file(out, sal, ent)
        assert report.ok, report.reasons[:5]
        assert report.entries_checked == 3


def test_blank_template_fails_with_empty_roster(tmp_path):
    sal, ent = mini_pair("classic")
    report = check_file(ent, sal, ent)
    assert not report.ok
    assert any("empty roster" in r and "7000000003" in r for r in report.reasons)


def test_hand_edited_cap_overrun_is_caught(tmp_path):
    sal, ent, out, pool = _written("classic", tmp_path)
    # Entry 2 sits exactly at $50,000; Palmieri (4300) -> Boeser (5100) makes it 50,800.
    _edit(out, _cell(pool, "Kyle Palmieri"), _cell(pool, "Brock Boeser"))
    report = check_file(out, sal, ent)
    assert not report.ok
    assert any("7000000002" in r and "salary 50800" in r for r in report.reasons)


def test_duplicated_person_is_caught(tmp_path):
    sal, ent, out, pool = _written("showdown", tmp_path)
    # Entry 102 has CPT Pettersson C; put Pettersson C's FLEX row in place of Blueger.
    line = physical_lines(out.read_bytes())[1][2]
    pet_c_flex = next(r for r in pool.rows if r.name == "Elias Pettersson" and r.position == "C" and "FLEX" in r.roster_positions)
    new_line = line.replace(_cell(pool, "Teddy Blueger", "FLEX"), f"{pet_c_flex.name} ({pet_c_flex.role_id})".encode())
    _edit(out, line, new_line)
    report = check_file(out, sal, ent)
    assert not report.ok
    assert any("7000000102" in r and "same person" in r for r in report.reasons)


def test_flex_id_in_cpt_slot_fails_and_cpt_id_passes(tmp_path):
    sal, ent, out, pool = _written("showdown", tmp_path)
    assert check_file(out, sal, ent).ok
    aho = next(r for r in pool.rows if r.name == "Sebastian Aho" and r.team == "CAR" and "CPT" in r.roster_positions)
    aho_flex = next(r for r in pool.rows if r.name == "Sebastian Aho" and r.team == "CAR" and "FLEX" in r.roster_positions)
    _edit(out, f"{aho.name} ({aho.role_id})".encode(), f"{aho_flex.name} ({aho_flex.role_id})".encode())
    report = check_file(out, sal, ent)
    assert any("7000000101" in r and "slot 0 CPT" in r for r in report.reasons)


def test_swapped_entry_order_is_caught(tmp_path):
    sal, ent, out, _ = _written("classic", tmp_path)
    bom, lines = physical_lines(out.read_bytes())
    lines[1], lines[2] = lines[2], lines[1]
    out.write_bytes((b"\xef\xbb\xbf" if bom else b"") + b"".join(lines))
    report = check_file(out, sal, ent)
    assert not report.ok
    assert any("order" in r for r in report.reasons)


def test_wrong_mode_file_is_rejected(tmp_path):
    sal_c, ent_c, out_c, _ = _written("classic", tmp_path)
    sal_s, _ = mini_pair("showdown")
    report = check_file(out_c, sal_s, ent_c)
    assert not report.ok
    assert any("salary file is showdown" in r for r in report.reasons)


def test_name_mismatch_unknown_id_and_ambiguous_id_are_caught(tmp_path):
    sal, ent, out, pool = _written("classic", tmp_path)
    good = out.read_bytes()
    palmieri = next(r for r in pool.rows if r.name == "Kyle Palmieri")
    _edit(out, _cell(pool, "Kyle Palmieri"), f"Kyle Palmero ({palmieri.role_id})".encode())
    assert any("does not match" in r for r in check_file(out, sal, ent).reasons)

    out.write_bytes(good)
    _edit(out, _cell(pool, "Kyle Palmieri"), b"Kyle Palmieri (12345678)")
    assert any("not in the salary file" in r for r in check_file(out, sal, ent).reasons)

    out.write_bytes(good)
    fast_id = sorted(pool.excluded_role_ids)[0]
    _edit(out, _cell(pool, "Kyle Palmieri"), f"Jesper Fast ({fast_id})".encode())
    assert any("ambiguous identity" in r for r in check_file(out, sal, ent).reasons)


def test_non_roster_edit_is_caught(tmp_path):
    sal, ent, out, _ = _written("classic", tmp_path)
    _edit(out, b"NHL Single Entry $1 Double Up", b"NHL Single Entry $1 Double Upp")
    assert any("non-roster fields changed" in r for r in check_file(out, sal, ent).reasons)
    sal, ent, out, _ = _written("classic", tmp_path)
    _edit(out, b"4. Use data from", b"4. use data from")
    assert any("non-entry line" in r for r in check_file(out, sal, ent).reasons)


def test_locked_cells_are_enforced(tmp_path):
    sal, ent, out, pool = _written("classic", tmp_path)
    mcdavid = _cell(pool, "Connor McDavid").decode()
    assert check_file(out, sal, ent, locked={("7000000001", 0): mcdavid}).ok
    report = check_file(out, sal, ent, locked={("7000000001", 0): "Leon Draisaitl (90000110)"})
    assert any("locked cell 0" in r for r in report.reasons)
    _edit(out, mcdavid.encode(), _cell(pool, "Leon Draisaitl"))
    assert not check_file(out, sal, ent, locked={("7000000001", 0): mcdavid}).ok


def test_parent_binding(tmp_path):
    sal, ent, out, pool = _written("classic", tmp_path)
    child = tmp_path / "child.csv"
    child.write_bytes(out.read_bytes())
    report = check_file(child, sal, ent, parent_path=out)
    assert report.ok and report.parent_sha256 is not None
    bom, lines = physical_lines(child.read_bytes())
    lines[1], lines[2] = lines[2], lines[1]
    child.write_bytes(b"".join(lines))
    assert any("parent" in r for r in check_file(child, sal, ent, parent_path=out).reasons)


def test_draft_group_mismatch_is_caught(tmp_path):
    sal, ent, out, pool = _written("classic", tmp_path)
    other_sal = tmp_path / "DKSalaries.csv"
    raw = sal.read_bytes()
    lines = raw.split(b"\r\n")
    other_sal.write_bytes(b"\r\n".join(lines[:-2] + lines[-1:]))  # drop the last player row
    report = check_file(out, other_sal, ent)
    assert any("draft group mismatch" in r for r in report.reasons)


def test_verify_cli_exit_codes(tmp_path, capsys):
    sal, ent, out, _ = _written("classic", tmp_path)
    assert cli.main(["verify", "--salary", str(sal), "--entries", str(ent), "--out", str(out)]) == 0
    assert "FILE_VALID=TRUE" in capsys.readouterr().out
    assert cli.main(["verify", "--salary", str(sal), "--entries", str(ent), "--out", str(ent)]) == 1
    printed = capsys.readouterr().out
    assert "FILE_VALID=FALSE" in printed and "empty roster" in printed


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_blank_template_reports_empty_roster_and_binds_draft_group(mode):
    sal, ent = real_pair(mode)
    report = check_file(ent, sal, ent)
    if report.ok:
        return  # a template with every lineup already filled is valid as downloaded
    assert all("empty roster" in r for r in report.reasons), report.reasons[:5]
    assert not any("draft group" in r for r in report.reasons)
    assert not report.notes, report.notes


def test_referee_imports_nothing_from_the_builder():
    forbidden = ("nhl_dfs.intake", "nhl_dfs.export", "nhl_dfs.contracts")
    for path in (REPO_ROOT / "src" / "nhl_dfs" / "referee").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
