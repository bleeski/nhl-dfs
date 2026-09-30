"""Ben, 2026-09-30: DraftKings' salary file marks each team's starting goalie Starting=P. A team's other goalies are
left out of the build pool, and in late swap and refresh they are NOT STARTING (repair targets), below an accepted
override or a Daily Faceoff confirmation. Lock semantics stay C2c's."""
import csv
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import TESTS
from nhl_dfs.build import goalies, late_swap
from nhl_dfs.build.run import dk_backup_goalies, dk_goalie_starters, run_slate
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary

pytestmark = [pytest.mark.c2c, pytest.mark.c9]

LS = TESTS / "fixtures" / "late_swap" / "classic"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
NO_NEWS = ({}, [], "none", [], {"source": "none", "fetched_utc": None, "age_min": None})


def with_starting(dst, marks: dict[str, str], status: dict[str, str] | None = None):
    """The fixture's salary file with Starting set for the named goalies (and optional Status values)."""
    rows = list(csv.reader(io.StringIO((LS / "DKSalaries.csv").read_bytes().decode("utf-8-sig"), newline="")))
    for rec in rows[1:]:
        if rec and rec[2] in marks:
            rec[10] = marks[rec[2]]
        if rec and status and rec[2] in status:
            rec[9] = status[rec[2]]
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    dst.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    return dst


def g1_marks():
    pool = read_salary(LS / "DKSalaries.csv")
    return {r.name: "P" for r in pool.rows if r.is_goalie and r.name.endswith("G1")}


def test_one_p_goalie_per_team_names_the_starter_and_ambiguous_or_out_marks_are_ignored(tmp_path):
    pool = read_salary(with_starting(tmp_path / "a.csv", g1_marks()))
    starters = dk_goalie_starters(pool)
    assert len(starters) == len(pool.teams)
    assert all(pool.persons[k].classic.name.endswith("G1") for k in starters.values())
    assert {pool.persons[k].classic.name for k in dk_backup_goalies(pool)} == \
        {r.name for r in pool.rows if r.is_goalie and r.name.endswith("G2")}
    team = next(r.team for r in pool.rows if r.name == "Alpha G1")
    both = read_salary(with_starting(tmp_path / "b.csv", {**g1_marks(), "Alpha G2": "P"}))
    assert team not in dk_goalie_starters(both)  # two P goalies: no DraftKings starter for that team
    out = read_salary(with_starting(tmp_path / "c.csv", g1_marks(), {"Alpha G1": "OUT"}))
    assert team not in dk_goalie_starters(out)  # a P goalie listed OUT: the flag is stale
    assert dk_goalie_starters(read_salary(LS / "DKSalaries.csv")) == {}  # no P anywhere: nothing changes


def test_the_build_leaves_the_other_goalies_out_of_every_lineup(tmp_path):
    sal = with_starting(tmp_path / "DKSalaries.csv", g1_marks())
    r = run_slate(sal, LS / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs", clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    pool = read_salary(sal)
    backups = dk_backup_goalies(pool)
    for e in read_entries(r.run.version_file(1)).entries:
        assert not {pool.by_role_id[cell_role_id(c)].person_key for c in e.cells} & backups
    assert r.manifest["news"]["csv"]["excluded_dk_backup_goalies"]
    assert "Starting=P" in r.manifest["news"]["csv_summary"]


def test_late_swap_repairs_an_open_cell_holding_a_goalie_draftkings_does_not_start(tmp_path):
    b = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: BEFORE)
    pool = read_salary(LS / "DKSalaries.csv")
    cur = b.run.version_file(1)
    used = {pool.by_role_id[cell_role_id(c)] for e in read_entries(cur).entries for c in e.cells}
    g = next(x for x in used if x.is_goalie)  # mark the OTHER goalie of this one's team as DraftKings' starter
    other = next(x for x in pool.rows if x.is_goalie and x.team == g.team and x.person_key != g.person_key)
    fresh = with_starting(tmp_path / "DKSalaries (1).csv", {other.name: "P"})
    s = late_swap.run(b.run.run_id, cur, offline=True, fast=True, runs_root=tmp_path / "runs", salary_path=fresh,
                      as_of=BEFORE)
    assert s.statuses["FILE_VALID"] == "TRUE", s.messages
    after = {cell_role_id(c) for e in read_entries(s.run.version_file(1)).entries for c in e.cells}
    assert g.role_id not in after  # every open cell holding him was repaired
    assert "Starting=P" in str(s.manifest["goalies"])  # the goalie table names DraftKings' flag as the source


def test_an_accepted_override_outranks_the_draftkings_flag():
    pool = read_salary(LS / "DKSalaries.csv")
    raw = pool.raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw, newline="")))
    for rec in rows[1:]:
        if rec and rec[2] == "Alpha G1":
            rec[10] = "P"
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    from dataclasses import replace

    marked = replace(pool, raw=buf.getvalue().encode("utf-8"))
    g1 = next(r for r in pool.rows if r.name == "Alpha G1")
    g2 = next(r for r in pool.rows if r.name == "Alpha G2")
    now = BEFORE
    b = goalies.build(marked, now=now, offline=True, inputs=NO_NEWS, csv_status={})
    assert b.persons[g1.person_key].status == goalies.EXPECTED and b.persons[g2.person_key].status == goalies.NOT_STARTING
    assert b.persons[g2.person_key].repair and "Starting=P" in b.persons[g2.person_key].source
    ovr = SimpleNamespace(role_id=g2.role_id, field="goalie_start", new=True, effective_utc=now - timedelta(hours=1),
                          source_url="https://example.test/confirmed")
    b2 = goalies.build(marked, now=now, offline=True, inputs=NO_NEWS, csv_status={}, overrides=[ovr])
    assert b2.persons[g2.person_key].status == goalies.CONFIRMED and b2.persons[g1.person_key].status == goalies.NOT_STARTING
    assert any("DraftKings marks Alpha G1" in p for p in b2.problems)  # the disagreement is reported
