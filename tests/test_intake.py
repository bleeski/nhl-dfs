import hashlib
from datetime import datetime, timezone

import pytest
from conftest import mini_pair, real_pair

from nhl_dfs.contracts.geometry import CLASSIC_SLOTS, Mode
from nhl_dfs.intake.entries import (
    canonical_to_template,
    cell_role_id,
    existing_lineup,
    read_entries,
    template_permutation,
    template_to_canonical,
)
from nhl_dfs.intake.salary import parse_game_info, read_salary

pytestmark = pytest.mark.c0b


def _kinds(pool, kind):
    return [c for c in pool.conflicts if c.kind == kind]


def test_mini_classic_pool_mode_rows_and_flags():
    pool = read_salary(mini_pair("classic")[0])
    assert pool.mode is Mode.CLASSIC
    assert len(pool.rows) == 32  # 34 rows minus the two overflow Jesper Fast rows
    assert pool.teams == {"VAN", "EDM", "NYI", "CAR"}
    mcdavid = next(r for r in pool.rows if r.name == "Connor McDavid")
    assert mcdavid.roster_positions == frozenset({"C", "UTIL"})
    flags = {r.name: (r.appg_raw, r.appg_flag) for r in pool.rows}
    assert flags["Vasily Podkolzin"] == (None, "MISSING")
    assert flags["Kasperi Kapanen"] == (0.0, "APPG_ZERO")
    assert flags["Ilya Sorokin"] == (-1.4, "VALUE")
    assert pool.sha256 == hashlib.sha256(mini_pair("classic")[0].read_bytes()).hexdigest()


def test_triple_overflow_is_excluded_and_reported():
    pool = read_salary(mini_pair("classic")[0])
    dup = _kinds(pool, "DUPLICATE_ROLE")
    assert len(dup) == 1 and dup[0].excluded and len(dup[0].role_ids) == 2
    for rid in dup[0].role_ids:
        assert rid not in pool.by_role_id
    assert pool.excluded_role_ids == frozenset(dup[0].role_ids)


@pytest.mark.parametrize(
    "mode, aho_d_team",
    [("classic", "NYI"), ("showdown", "VAN")],
)
def test_pettersson_and_aho_are_reported_but_never_merged(mode, aho_d_team):
    pool = read_salary(mini_pair(mode)[0])
    same = {c.detail.split(":")[0] for c in _kinds(pool, "SAME_NAME")}
    assert same == {"elias pettersson", "sebastian aho"}
    assert all(not c.excluded for c in _kinds(pool, "SAME_NAME"))
    keys = {k for k in pool.persons if k.startswith(("elias pettersson|", "sebastian aho|"))}
    assert keys == {
        "elias pettersson|VAN|F",
        "elias pettersson|VAN|D",
        "sebastian aho|CAR|F",
        f"sebastian aho|{aho_d_team}|D",
    }


def test_person_key_collision_excludes_both_rows(tmp_path):
    # Different raw triples (LW vs C) that normalize to one person key (F): ambiguous, so excluded.
    raw = mini_pair("classic")[0].read_bytes().decode("utf-8-sig")
    extra = (
        "LW,Sebastian Aho (90000999),Sebastian Aho,90000999,W/UTIL,3000,"
        "NYI@CAR 09/29/2026 07:00PM ET,CAR,2,,\r\n"
    )
    p = tmp_path / "DKSalaries.csv"
    p.write_bytes(("﻿" + raw + extra).encode("utf-8"))
    pool = read_salary(p)
    collision = _kinds(pool, "PERSON_KEY_COLLISION")
    assert len(collision) == 1 and collision[0].excluded
    aho_c = next(c for c in read_salary(mini_pair("classic")[0]).rows if c.person_key == "sebastian aho|CAR|F")
    assert set(collision[0].role_ids) == {"90000999", aho_c.role_id}
    assert "90000999" not in pool.by_role_id and aho_c.role_id not in pool.by_role_id
    assert not any("90000999" in c.role_ids for c in _kinds(pool, "DUPLICATE_ROLE"))


def test_real_fixture_absence_skips_loudly(tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest, "REAL", tmp_path)
    with pytest.warns(UserWarning, match="REAL FIXTURE MISSING"):
        with pytest.raises(pytest.skip.Exception, match="REAL FIXTURE MISSING"):
            conftest.real_pair("classic")


def test_mini_showdown_pairing_and_warnings():
    pool = read_salary(mini_pair("showdown")[0])
    assert pool.mode is Mode.SHOWDOWN
    pet_c = pool.persons["elias pettersson|VAN|F"]
    assert pet_c.cpt.roster_positions == frozenset({"CPT"})
    assert pet_c.flex.roster_positions == frozenset({"FLEX"})
    assert pet_c.cpt.salary == 14400 and pet_c.flex.salary == 9600
    ratio = _kinds(pool, "CPT_SALARY_RATIO")
    assert [c.detail.split(":")[0] for c in ratio] == ["conor garland|VAN|F"]
    appg = _kinds(pool, "CPT_APPG_MISMATCH")
    assert [c.detail.split(":")[0] for c in appg] == ["jordan martinook|CAR|F"]
    # The file's numbers are never rewritten to hide a warning.
    garland = pool.persons["conor garland|VAN|F"]
    assert (garland.cpt.salary, garland.flex.salary) == (9100, 6000)
    staal = [c for c in _kinds(pool, "DUPLICATE_ROLE") if "Jordan Staal" in c.detail]
    assert len(staal) == 1 and len(staal[0].role_ids) == 3
    assert "jordan staal|CAR|F" not in pool.persons


def test_game_info_parses_to_exact_utc():
    key, info = parse_game_info("CHI@VGK 09/29/2026 10:30PM ET")
    assert key == "CHI@VGK" and info.away == "CHI" and info.home == "VGK"
    assert info.start_utc == datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)
    assert info.start_et_text == "09/29/2026 10:30PM ET"
    _, early = parse_game_info("NYI@CAR 09/29/2026 07:00PM ET")
    assert early.start_utc == datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_game_info("Postponed")


def test_missing_required_column_is_rejected(tmp_path):
    raw = mini_pair("classic")[0].read_bytes().replace(b"AvgPointsPerGame", b"AvgPts", 1)
    p = tmp_path / "DKSalaries.csv"
    p.write_bytes(raw)
    with pytest.raises(ValueError, match="AvgPointsPerGame"):
        read_salary(p)


def test_mini_classic_entries_structure():
    path = mini_pair("classic")[1]
    ef = read_entries(path)
    assert ef.mode is Mode.CLASSIC
    assert ef.bom is False and ef.newline == "\r\n"
    assert ef.roster_labels == ("C", "C", "W", "W", "W", "D", "D", "G", "UTIL")
    assert ef.roster_cols == list(range(4, 13))
    assert [e.entry_id for e in ef.entries] == ["7000000001", "7000000002", "7000000003"]
    assert ef.entries[0].contest_name == "NHL $5K Sniper, 150 Max"
    assert ef.entries[2].contest_name.endswith("] ")
    # instructions 4 and 5 (1-3 share the entry lines), blank, list header, 34 players
    assert len(ef.tail_lines) == 2 + 1 + 1 + 34
    assert b"".join(e.line_bytes for e in ef.entries) in ef.raw


def test_existing_lineups_in_both_cell_formats_map_to_canonical_order():
    pool = read_salary(mini_pair("classic")[0])
    ef = read_entries(mini_pair("classic")[1])
    e1 = existing_lineup(ef.entries[0], ef)  # "Name (ID)" cells
    e2 = existing_lineup(ef.entries[1], ef)  # bare IDs
    assert existing_lineup(ef.entries[2], ef) == (None,) * 9
    for lineup in (e1, e2):
        rows = [pool.by_role_id[rid] for rid in lineup]
        for slot, row in zip(CLASSIC_SLOTS, rows):
            assert slot in row.roster_positions or slot == "UTIL"
        assert rows[8].is_goalie  # canonical G is last; template had G before UTIL
    assert cell_role_id("Drew O'Connor (90000108)") == "90000108"
    assert cell_role_id(" 90000108 ") == "90000108"
    with pytest.raises(ValueError):
        cell_role_id("Drew O'Connor")


def test_template_permutation_round_trips():
    labels = ("C", "C", "W", "W", "W", "D", "D", "G", "UTIL")
    canon = tuple(f"x{j}" for j in range(9))
    tmpl = canonical_to_template(canon, labels, Mode.CLASSIC)
    assert tmpl[7] == "x8" and tmpl[8] == "x7"
    assert template_to_canonical(tmpl, labels, Mode.CLASSIC) == canon
    assert template_permutation(("CPT",) + ("FLEX",) * 5, Mode.SHOWDOWN) == tuple(range(6))
    with pytest.raises(ValueError):
        template_permutation(("C",) * 9, Mode.CLASSIC)


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_files_parse(mode):
    sal_path, ent_path = real_pair(mode)
    pool = read_salary(sal_path)
    ef = read_entries(ent_path)
    assert pool.mode.value == mode and ef.mode.value == mode
    assert ef.entries, "real template has no reserved entries"
    assert pool.sha256 == hashlib.sha256(sal_path.read_bytes()).hexdigest()
    assert ef.sha256 == hashlib.sha256(ent_path.read_bytes()).hexdigest()
    assert pool.games and all(g.start_utc.tzinfo is timezone.utc for g in pool.games.values())
    if mode == "showdown":
        unpaired = {rid for c in pool.conflicts if c.kind == "UNPAIRED" for rid in c.role_ids}
        for p in pool.persons.values():
            assert (p.cpt and p.flex) or {r.role_id for r in (p.cpt, p.flex) if r} <= unpaired


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_name_plus_id_matches_the_written_cell_format(mode):
    import csv
    import io

    from nhl_dfs.export.writer import format_cell

    sal_path, _ = real_pair(mode)
    pool = read_salary(sal_path)
    records = list(csv.reader(io.StringIO(sal_path.read_bytes().decode("utf-8-sig"), newline="")))
    col_name_id, col_id = records[0].index("Name + ID"), records[0].index("ID")
    file_cells = {rec[col_id]: rec[col_name_id] for rec in records[1:] if rec}
    mismatched = [rid for rid, row in pool.by_role_id.items() if format_cell(row) != file_cells[rid]]
    assert not mismatched, f"{len(mismatched)} rows differ from the file's Name + ID, e.g. {mismatched[:3]}"


def test_real_showdown_start_time_is_exact_utc():
    pool = read_salary(real_pair("showdown")[0])
    if "CHI@VGK" not in pool.games:
        pytest.skip("start-time spot check is specific to the 2026-09-29 CHI@VGK fixture")
    info = pool.games["CHI@VGK"]
    assert info.start_utc == datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)
