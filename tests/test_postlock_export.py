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


def test_an_initial_run_with_the_pre_lock_salary_file_takes_the_started_game_from_the_export(tmp_path):
    """Review of commit 1: the salary file and the clock both call game 1 open; only the export says it started."""
    r = run_slate(CLASSIC_SALARY, EXPORT, offline=True, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs",
                  clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:5]
    before, after = cells(EXPORT), cells(r.public_path)
    for eid, k, text in marked_cells(EXPORT):
        assert after[eid][k] == text
    pool = read_salary(CLASSIC_SALARY)
    added = {cell_role_id(a) for eid in after for a, b in zip(after[eid], before[eid]) if a != b}
    assert added and not {pool.by_role_id[x].team for x in added} & G1_TEAMS


def test_the_embedded_in_progress_rows_alone_start_their_game(tmp_path):
    """No cell marked LOCKED: the embedded list's In Progress rows still start AAA@BBB whatever the clock says."""
    plain = tmp_path / "plain.csv"
    plain.write_bytes(EXPORT.read_bytes().replace(b" (LOCKED)", b""))
    cur, pool = read_entries(plain), read_salary(CLASSIC_SALARY)
    assert not marked_cells(plain) and len(embedded_started_ids(cur)) == 24
    ls = locks_mod.compute(cur, pool, None, BEFORE, 300)
    assert "AAA@BBB" in ls.started_games and "CCC@DDD" not in ls.started_games
    assert ls.not_addable >= {x.role_id for x in pool.rows if x.team in G1_TEAMS}
    assert not any(c.dk_locked for c in ls.cells.values())


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


# -- versioned delivery files (B119, flags 81, 84) ---------------------------------------------------------------------

SLATE = "classic-20261015-test"


def _bare_run(tmp_path, name="runs"):
    from nhl_dfs.build.state import new_run
    from nhl_dfs.contracts.geometry import Mode

    run = new_run(tmp_path / name, mode=Mode.CLASSIC, clock=lambda: BEFORE)
    (run.inputs / "DKSalaries.csv").write_bytes(b"salary")
    (run.inputs / "DKEntries.csv").write_bytes(b"entries")
    return run


def _report(data: bytes):
    from nhl_dfs.build.state import sha256
    from nhl_dfs.referee.check_file import RefereeReport

    return RefereeReport(ok=True, reasons=[], mode="classic", entries_checked=1, out_sha256=sha256(data),
                         salary_sha256=sha256(b"salary"), entries_sha256=sha256(b"entries"), parent_sha256=None)


def _numbered(out_dir) -> dict[int, str]:
    from nhl_dfs.build.state import DELIVERY_RE

    return {int(m.group(1)): p.name for p in out_dir.iterdir() if (m := DELIVERY_RE.match(p.name))}


def test_two_publishes_of_one_run_leave_two_numbered_files_and_the_plain_file_is_the_newest(tmp_path):
    import json

    from nhl_dfs.build.state import publish, sha256

    run, out = _bare_run(tmp_path), tmp_path / "outputs"
    r1 = publish(run, b"one", _report(b"one"), SLATE, outputs_root=out)
    r2 = publish(run, b"two", _report(b"two"), SLATE, outputs_root=out)
    d = out / SLATE
    assert _numbered(d) == {1: f"DKEntries_v1_{sha256(b'one')[:8]}.csv", 2: f"DKEntries_v2_{sha256(b'two')[:8]}.csv"}
    assert (r1.delivery_version, r2.delivery_version) == (1, 2) and r2.delivery_path == d / _numbered(d)[2]
    assert (d / _numbered(d)[1]).read_bytes() == b"one" and (d / _numbered(d)[2]).read_bytes() == b"two"
    assert (d / "DKEntries.csv").read_bytes() == b"two"  # the plain file equals the newest numbered file
    stamp = json.loads((d / "published.json").read_text())
    assert stamp["delivery_version"] == 2 and stamp["delivery_name"] == _numbered(d)[2] and stamp["version"] == 2


def test_a_second_run_of_the_slate_continues_the_count_while_its_own_folder_starts_at_v1(tmp_path):
    from nhl_dfs.build.state import publish

    a, b, out = _bare_run(tmp_path, "a"), _bare_run(tmp_path, "b"), tmp_path / "outputs"
    publish(a, b"one", _report(b"one"), SLATE, outputs_root=out)
    publish(a, b"two", _report(b"two"), SLATE, outputs_root=out)
    r = publish(b, b"three", _report(b"three"), SLATE, outputs_root=out)
    assert r.version == 1 and b.version_file(1).read_bytes() == b"three"
    assert r.delivery_version == 3 and sorted(_numbered(out / SLATE)) == [1, 2, 3]


def test_a_stale_publish_writes_no_numbered_file(tmp_path):
    from nhl_dfs.build.state import publish, sha256

    a, b, out = _bare_run(tmp_path, "a"), _bare_run(tmp_path, "b"), tmp_path / "outputs"
    publish(a, b"one", _report(b"one"), SLATE, outputs_root=out)
    publish(b, b"newer", _report(b"newer"), SLATE, outputs_root=out)
    r = publish(a, b"stale", _report(b"stale"), SLATE, outputs_root=out, expect_public_sha=sha256(b"one"))
    assert not r.public_replaced and r.delivery_version is None and r.delivery_path is None
    d = out / SLATE
    assert sorted(_numbered(d)) == [1, 2] and (d / "DKEntries.csv").read_bytes() == b"newer" == (d / _numbered(d)[2]).read_bytes()


def test_a_slate_published_before_c53_continues_from_its_version(tmp_path):
    import json

    from nhl_dfs.build.state import publish

    d = tmp_path / "outputs" / SLATE
    d.mkdir(parents=True)
    (d / "published.json").write_text(json.dumps({"run_id": "x", "version": 5, "sha256": "0" * 64}))
    r = publish(_bare_run(tmp_path), b"six", _report(b"six"), SLATE, outputs_root=tmp_path / "outputs")
    assert r.delivery_version == 6


def test_late_swap_delivers_the_next_number_and_verify_names_it(tmp_path, capsys):
    from nhl_dfs import cli

    b = parent(tmp_path)
    r = swap(tmp_path, b, EXPORT)
    v = r.manifest["versions"][-1]
    n_parent = b.manifest["versions"][-1]["delivery_version"]
    assert v["delivery_version"] == n_parent + 1 and v["version"] == 1
    dpath = tmp_path / "outputs" / r.slate_id / f"DKEntries_v{v['delivery_version']}_{v['sha256'][:8]}.csv"
    assert v["delivery_path"] == str(dpath) and dpath.read_bytes() == r.run.version_file(1).read_bytes()
    assert (dpath.parent / "DKEntries.csv").read_bytes() == dpath.read_bytes()
    assert dpath.name in (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    ok, lines = cli.verify_run(tmp_path / "runs", r.run.run_id, now=BEFORE)
    assert ok and f"delivery_file={dpath} (v{v['delivery_version']} sha256 {v['sha256'][:8]})" in lines
    assert "DELIVERY_CURRENT=TRUE" in lines and any(x.startswith("LOCK_SOON=NONE") for x in lines)
    # the repaired cell removed Echo C1, whose game (EEE@FFF, 10:00 PM ET) is 20 minutes away
    g3 = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)
    _, soon = cli.verify_run(tmp_path / "runs", r.run.run_id, now=g3 - timedelta(minutes=20))
    assert any(x.startswith("LOCK_SOON=EEE@FFF locks 2026-10-15 09:00:00 PM CDT (in 20 min)") for x in soon), soon
    _, older = cli.verify_run(tmp_path / "runs", b.run.run_id, now=BEFORE)  # the parent's file is no longer current
    assert any(x.startswith("DELIVERY_CURRENT=FALSE") for x in older)
    assert cli.main(["verify", "--run", r.run.run_id, "--runs-root", str(tmp_path / "runs")]) == 0
    assert f"delivery_file={dpath}" in capsys.readouterr().out


def test_a_zero_change_late_swap_still_publishes_the_next_number(tmp_path):
    """Flag 81 (c), a deviation from the card's wording: the file equals DraftKings' export and the output says
    there is nothing new to upload, but it is published, so a later QA round or Ben override on this run delivers."""
    b = parent(tmp_path)
    cur = without_entry(EXPORT, tmp_path / "cur.csv", OUT_ENTRY)
    r = swap(tmp_path, b, cur)
    v = r.manifest["versions"][-1]
    assert r.manifest["changed_cells"] == [] and any("nothing new to upload" in m for m in r.messages)
    assert v["delivery_version"] == b.manifest["versions"][-1]["delivery_version"] + 1
    assert open(v["delivery_path"], "rb").read() == cur.read_bytes()


def test_a_rehearsal_is_never_the_current_delivery(tmp_path):
    from nhl_dfs import cli

    b = parent(tmp_path)
    r = late_swap.run(b.run.run_id, EXPORT, offline=True, fast=True, runs_root=tmp_path / "runs",
                      outputs_root=tmp_path / "outputs", salary_path=SALARY, as_of=AFTER_G1)
    assert r.statuses["FILE_VALID"] == "TRUE" and str(r.run.path) in r.manifest["versions"][-1]["delivery_path"]
    _, lines = cli.verify_run(tmp_path / "runs", r.run.run_id, now=BEFORE)
    assert "DELIVERY_CURRENT=FALSE (rehearsal: do not upload)" in lines
