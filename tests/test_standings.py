"""C11: DraftKings standings exports: parse, lineup slots, ownership blocks, joins to the salary pool."""
import dataclasses
import zipfile
from decimal import Decimal

import pytest

from conftest import TESTS, real_pair
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.learn import standings as st
from pool_builder import make_pool, row, sd_person

pytestmark = pytest.mark.c11

FIX = TESTS / "fixtures" / "standings"
CLASSIC = FIX / "classic" / "contest-standings-195958173.csv"
SHOWDOWN = FIX / "showdown" / "contest-standings-196048725.csv"
HDR = "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\r\n"


def named(r, name):
    return dataclasses.replace(r, name=name)


def csv_bytes(lines):
    return ("﻿" + HDR + "".join(line + "\r\n" for line in lines)).encode("utf-8")


def test_fixtures_parse_for_both_modes_with_our_entries_ties_and_blank_lineups():
    c = st.read(CLASSIC)
    assert (c.contest_id, c.mode) == (195958173, Mode.CLASSIC) and len(c.entries) <= 60
    mine = c.by_entry()["5274444880"]
    assert mine.entry_name == "bleeski" and mine.rank == 532 and mine.points == Decimal("95.6")
    assert c.tied(532) == 3 and [s for s, _ in mine.lineup] == list(st.CLASSIC_SEQ)
    assert ("G", "Adin Hill") in mine.lineup
    assert any(e.blank for e in c.entries)
    s = st.read(SHOWDOWN)
    assert (s.contest_id, s.mode) == (196048725, Mode.SHOWDOWN)
    e = s.by_entry()["5274453163"]
    assert e.rank == 101 and e.lineup[0] == ("CPT", "Jack Eichel") and s.tied(101) == 1
    assert sum(x.blank for x in s.entries) == 2
    # CPT and FLEX ownership rows are separate, and a CPT row's FPTS is 1.5 x the FLEX row's
    by = {}
    for x in s.ownership:
        by.setdefault(x.name, {})[x.roster_position] = x
    both = [v for v in by.values() if {"CPT", "FLEX"} <= set(v)]
    assert both and all(v["CPT"].fpts == v["FLEX"].fpts * Decimal("1.5") for v in both)
    assert not any("1.5 x FLEX" in n for n in s.notes)
    # no other entrant's username survives the trimming
    assert {x.entry_name.split(" (")[0] for x in c.entries + s.entries} - {"bleeski"} <= {
        f"entrant{i:04d}" for i in range(1, 61)}


def test_lineups_follow_the_slot_sequence_and_names_may_contain_spaces():
    lu = st._parse_lineup("C Connor McDavid C J.T. Miller D Evan Bouchard D Michael Del Zotto G Jean-Francois Berube "
                          "UTIL Leon Draisaitl W Alex DeBrincat W Kirill Kaprizov W Tim Stutzle", Mode.CLASSIC)
    assert [s for s, _ in lu] == list(st.CLASSIC_SEQ)
    assert lu[1] == ("C", "J.T. Miller") and lu[3] == ("D", "Michael Del Zotto") and lu[4] == ("G", "Jean-Francois Berube")
    lu = st._parse_lineup("CPT Jack Eichel FLEX Carter Hart FLEX Mark Stone FLEX A FLEX B FLEX Jean-Luc Van Der Berg",
                          Mode.SHOWDOWN)
    assert lu[0] == ("CPT", "Jack Eichel") and lu[-1] == ("FLEX", "Jean-Luc Van Der Berg")
    assert st._parse_lineup("", Mode.SHOWDOWN) == ()
    with pytest.raises(ValueError):
        st._parse_lineup("CPT Jack Eichel FLEX Carter Hart", Mode.SHOWDOWN)


def test_pettersson_resolves_by_position_token_and_a_same_team_same_position_collision_is_conflicted():
    rows = [named(row(1, "VAN", "C", 6000, person="elias pettersson|VAN|F"), "Elias Pettersson"),
            named(row(2, "VAN", "D", 4000, person="elias pettersson|VAN|D"), "Elias Pettersson"),
            named(row(3, "EDM", "C", 5000, person="john smith|EDM|F"), "John Smith"),
            named(row(4, "EDM", "C", 3000, person="john smith|EDM|F2"), "John Smith"),
            named(row(5, "EDM", "G", 7000), "Stuart Skinner")]
    pool = make_pool(Mode.CLASSIC, rows)
    data = csv_bytes(["1,1,a,0,10,,,Elias Pettersson,C,50.00%,10",
                      ",,,,,,,Elias Pettersson,D,25.00%,4",
                      ",,,,,,,Elias Pettersson,UTIL,5.00%,4",
                      ",,,,,,,John Smith,C,10.00%,2",
                      ",,,,,,,Stuart Skinner,G,40.00%,3"])
    s = st.parse(data, "contest-standings-1.csv")
    j = st.join(s, pool)
    assert j.points_tenths["elias pettersson|VAN|F"] == 100 and j.points_tenths["elias pettersson|VAN|D"] == 40
    assert any(c.startswith("Elias Pettersson (UTIL)") for c in j.conflicts)
    assert any(c.startswith("John Smith (C)") for c in j.conflicts)
    assert "john smith|EDM|F" not in j.points_tenths and not j.complete  # never guessed


def test_classic_ownership_sums_position_and_util_rows_from_the_lineups_and_reports_a_listing_gap():
    rows = [named(row(i, "AAA", p, 4000), f"P{i}") for i, p in enumerate(["C", "C", "D", "D", "G", "C", "LW", "RW", "LW"], start=1)]
    rows.append(named(row(10, "AAA", "C", 4000), "Bench"))
    pool = make_pool(Mode.CLASSIC, rows)
    lu = "C P1 C P2 D P3 D P4 G P5 UTIL P6 W P7 W P8 W P9"
    lu2 = "C P1 C P6 D P3 D P4 G P5 UTIL P2 W P7 W P8 W P9"
    own = [("P1", "C", "100.00%"), ("P2", "C", "50.00%"), ("P2", "UTIL", "50.00%"), ("P6", "UTIL", "50.00%")]
    lines = [f"1,11,a,0,50,{lu},,{own[0][0]},{own[0][1]},{own[0][2]},5", f"2,12,b,0,40,{lu2},,{own[1][0]},{own[1][1]},{own[1][2]},4"]
    lines += [f",,,,,,,{n},{p},{x},4" for n, p, x in own[2:]]  # P6's C row is missing from the listing
    j = st.join(st.parse(csv_bytes(lines), "contest-standings-2.csv"), pool)
    assert j.own["2"] == pytest.approx(100.0) and j.own["6"] == pytest.approx(100.0)  # position + UTIL, from lineups
    assert j.own.get("10", 0.0) == 0.0 and j.complete  # zero ownership observed from the complete lineups
    assert any("listed" in n and "P6" in n for n in j.notes)
    assert list(j.dup_counts.values()) == [2]  # the same nine persons in different slots: one Classic lineup, twice


def test_zip_and_folder_reads_keep_one_copy_of_a_contest(tmp_path):
    z = tmp_path / "contest-standings-196048725.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("contest-standings-196048725.csv", SHOWDOWN.read_bytes())
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "contest-standings-196048725.csv").write_bytes(SHOWDOWN.read_bytes())
    got, notes = st.read_all(tmp_path)
    assert [s.contest_id for s in got] == [196048725] and not notes
    assert st.read(z).raw_sha == st.read(SHOWDOWN).raw_sha


def test_the_real_pool_joins_our_fixture_entries():
    sal, _ = real_pair("showdown", "2026-09-29")
    pool = read_salary(sal)
    j = st.join(st.read(SHOWDOWN), pool)
    assert all(j.lineups["5274453163"]) and j.complete and not j.unmatched
    assert pool.by_role_id[j.lineups["5274453163"][0]].name == "Jack Eichel"
