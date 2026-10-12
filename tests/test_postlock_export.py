"""C53: DraftKings' post-lock export is readable (B118, B42 remainder).

The synthetic export under tests/fixtures/late_swap/postlock/ follows the 2026-10-11 format: a started cell reads
"Name (ID) (LOCKED)", the embedded player list sits beside the entries with the marker in Name + ID and
"In Progress" in Game Info. Ben's real export, when saved as tests/fixtures/real/<date>/classic/DKEntries.postlock.csv
next to that slate's DKSalaries.csv, runs the real test; it skips loudly otherwise. No network anywhere.
"""

import warnings
from datetime import datetime, timedelta, timezone

import pytest

from conftest import REAL, TESTS
from nhl_dfs.build import late_swap
from nhl_dfs.build import locks as locks_mod
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.statuses import CellLock
from nhl_dfs.intake.entries import cell_role_id, dk_locked, embedded_started_ids, parse_cell, read_entries, template_permutation
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import _cell_id, check_file

pytestmark = pytest.mark.c53

LS = TESTS / "fixtures" / "late_swap"
PL = LS / "postlock"
EXPORT = PL / "DKEntries.postlock.csv"
SALARY = PL / "DKSalaries.csv"
CLASSIC_SALARY = LS / "classic" / "DKSalaries.csv"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)  # AAA@BBB, the started game in the export
AFTER_G1 = G1 + timedelta(minutes=10)
G1_TEAMS = {"AAA", "BBB"}
OUT_ENTRY = "7300000001"  # holds Echo C1 (DK status OUT, game 3) in an open cell


def parent(tmp_path):
    r = run_slate(CLASSIC_SALARY, PL / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def swap(tmp_path, b, current, *, salary=SALARY, now=AFTER_G1):
    return late_swap.run(b.run.run_id, current, offline=True, fast=True, runs_root=tmp_path / "runs",
                         outputs_root=tmp_path / "outputs", salary_path=salary, clock=lambda: now)


def cells(path) -> dict[str, tuple[str, ...]]:
    return {e.entry_id: e.cells for e in read_entries(path).entries}


def marked_cells(path) -> list[tuple[str, int, str]]:
    return [(eid, k, c) for eid, row in cells(path).items() for k, c in enumerate(row) if dk_locked(c)]


def edit_cell(src, dst, entry_id: str, old: str, new: str):
    """A copy of src with one roster cell of one entry line replaced (the cell text occurs once on that line)."""
    raw = src.read_bytes()
    lines = raw.split(b"\r\n")
    hits = [i for i, x in enumerate(lines) if x.lstrip(b"\xef\xbb\xbf").startswith(entry_id.encode())]
    assert len(hits) == 1 and lines[hits[0]].count(old.encode()) == 1
    lines[hits[0]] = lines[hits[0]].replace(old.encode(), new.encode())
    dst.write_bytes(b"\r\n".join(lines))
    return dst


def without_entry(src, dst, entry_id: str):
    lines = src.read_bytes().split(b"\r\n")
    dst.write_bytes(b"\r\n".join(x for x in lines if not x.lstrip(b"\xef\xbb\xbf").startswith(entry_id.encode())))
    return dst


def test_cell_parsers_read_the_locked_marker():
    assert parse_cell("J.T. Miller (44471322) (LOCKED)") == ("44471322", True)
    assert parse_cell("J.T. Miller (44471322)") == ("44471322", False)
    assert parse_cell("44471322") == ("44471322", False) and parse_cell("44471322 (LOCKED)") == ("44471322", True)
    assert parse_cell("") == (None, False) and cell_role_id("  ") is None
    assert cell_role_id("J.T. Miller (44471322) (LOCKED)") == "44471322"  # never the id with the marker attached
    for junk in ("J.T. Miller", "(LOCKED)", "J.T. Miller (44471322) LOCKED", "J.T. Miller (LOCKED)"):
        with pytest.raises(ValueError):
            parse_cell(junk)
        assert not dk_locked(junk)
    # the referee's own parser, independent of intake
    assert _cell_id("J.T. Miller (44471322) (LOCKED)") == ("44471322", "J.T. Miller")
    assert _cell_id("44471322 (LOCKED)") == ("44471322", None)
    with pytest.raises(ValueError):
        _cell_id("(LOCKED)")


def test_the_synthetic_post_lock_export_reads_every_entry_and_passes_the_referee_as_is():
    cur = read_entries(EXPORT)
    pool = read_salary(SALARY)
    g1_ids = {r.role_id for r in pool.started_rows}
    assert len(cur.entries) == 8 and len(g1_ids) == 24
    marked = marked_cells(EXPORT)
    assert len(marked) == 22 and {cell_role_id(c) for _, _, c in marked} <= g1_ids
    assert embedded_started_ids(cur) == g1_ids  # the list beside entries 7 and 8 is found
    for now in (BEFORE, AFTER_G1):
        ls = locks_mod.compute(cur, pool, None, now, 300)
        assert not ls.unreadable_entries
        perm = template_permutation(cur.roster_labels, cur.mode)
        for eid, col, _ in marked:
            c = ls.cells[(eid, perm[col])]
            assert c.lock is CellLock.LOCKED and c.dk_locked and c.reason.startswith("started")
    rep = check_file(EXPORT, SALARY, EXPORT)  # embedded Name + ID with the marker, In Progress Game Info
    assert rep.ok, rep.reasons[:3]


def test_fast_late_swap_repairs_the_open_out_cell_and_keeps_every_marked_cell_byte_for_byte(tmp_path):
    b = parent(tmp_path)
    r = swap(tmp_path, b, EXPORT)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:5]
    out = r.run.version_file(1)
    before, after = cells(EXPORT), cells(out)
    for eid, k, text in marked_cells(EXPORT):
        assert after[eid][k] == text
    raw_out = out.read_bytes()
    for _, _, text in marked_cells(EXPORT):
        assert text.encode() in raw_out
    pool = read_salary(CLASSIC_SALARY)
    echo_c1 = next(x.role_id for x in pool.rows if x.name == "Echo C1")
    changed = r.manifest["changed_cells"]
    assert {c["entry_id"] for c in changed} == {OUT_ENTRY} and [c["from"] for c in changed] == [echo_c1]
    assert all(pool.by_role_id[c["to"]].team not in G1_TEAMS for c in changed)
    assert r.manifest["locks"]["dk_locked_cells"] == 22
    # every line but the repaired entry's is byte-identical to DraftKings' export
    a, b2 = EXPORT.read_bytes().split(b"\r\n"), raw_out.split(b"\r\n")
    assert len(a) == len(b2) and [i for i, (x, y) in enumerate(zip(a, b2)) if x != y] == [1]


def test_with_no_open_cell_to_repair_the_file_equals_the_export(tmp_path):
    b = parent(tmp_path)
    cur = without_entry(EXPORT, tmp_path / "cur.csv", OUT_ENTRY)
    r = swap(tmp_path, b, cur)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.manifest["changed_cells"] == []
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert any("nothing new to upload" in m for m in r.messages)


def test_a_marked_cell_is_pinned_even_when_its_player_is_out_and_the_clock_says_open(tmp_path):
    """The parent's pre-lock salary file and a clock before every game: only DraftKings' marker pins the cell."""
    b = parent(tmp_path)
    pool = read_salary(CLASSIC_SALARY)
    echo_c1 = next(x for x in pool.rows if x.name == "Echo C1")
    text = f"Echo C1 ({echo_c1.role_id})"
    cur = edit_cell(EXPORT, tmp_path / "cur.csv", OUT_ENTRY, text, text + " (LOCKED)")
    ls = locks_mod.compute(read_entries(cur), pool, None, BEFORE, 300)
    assert ls.not_addable >= {x.role_id for x in pool.rows if x.team in ({"EEE", "FFF"} | G1_TEAMS)}
    r = swap(tmp_path, b, cur, salary=CLASSIC_SALARY, now=BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:5]
    assert cells(r.run.version_file(1))[OUT_ENTRY] == cells(cur)[OUT_ENTRY]
    assert all(c["from"] != echo_c1.role_id for c in r.manifest["changed_cells"])


@pytest.mark.parametrize("kind", ["dropped", "changed", "forged"])
def test_the_referee_refuses_a_marker_on_a_changed_cell(tmp_path, kind):
    alpha_c1 = "Alpha C1 (81000001) (LOCKED)"
    if kind == "dropped":
        out = edit_cell(EXPORT, tmp_path / "out.csv", OUT_ENTRY, alpha_c1, "Alpha C1 (81000001)")
    elif kind == "changed":
        out = edit_cell(EXPORT, tmp_path / "out.csv", OUT_ENTRY, alpha_c1, "Alpha C2 (81000002) (LOCKED)")
    else:  # a marker written on a cell DraftKings did not lock
        first = cells(EXPORT)["7300000005"][0]
        out = edit_cell(EXPORT, tmp_path / "out.csv", "7300000005", first, first + " (LOCKED)")
    rep = check_file(out, SALARY, EXPORT)
    assert not rep.ok and any("LOCKED marker" in x for x in rep.reasons), rep.reasons


def test_an_initial_run_on_the_post_lock_export_keeps_the_marked_bytes(tmp_path):
    r = run_slate(SALARY, EXPORT, offline=True, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                  clock=lambda: AFTER_G1)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:5]
    after = cells(r.public_path)
    for eid, k, text in marked_cells(EXPORT):
        assert after[eid][k] == text


def test_real_post_lock_export_reads_and_passes_the_referee_cell_checks():
    """Ben's 2026-10-11 export (gitignored). Skips loudly until it is saved next to that slate's DKSalaries.csv."""
    hits = [p for p in sorted(REAL.glob("*/classic/DKEntries.postlock.csv")) if (p.parent / "DKSalaries.csv").exists()]
    if not hits:
        msg = ("REAL FIXTURE MISSING: tests/fixtures/real/<date>/classic/DKEntries.postlock.csv beside its DKSalaries.csv "
               "(Ben's DraftKings export after lock)")
        warnings.warn(msg)
        pytest.skip(msg)
    path = hits[-1]
    cur = read_entries(path)
    marked = [(e.entry_id, c) for e in cur.entries for c in e.cells if dk_locked(c)]
    assert marked, "the real post-lock export has no LOCKED cell"
    ls = locks_mod.compute(cur, read_salary(path.parent / "DKSalaries.csv"), None, datetime.now(timezone.utc), 300)
    assert not ls.unreadable_entries
    rep = check_file(path, path.parent / "DKSalaries.csv", path)
    assert not [x for x in rep.reasons if "unreadable" in x or "LOCKED marker" in x], rep.reasons[:5]
