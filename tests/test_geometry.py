import pytest

from nhl_dfs.contracts.geometry import (
    CLASSIC_SLOTS,
    SALARY_CAP,
    SHOWDOWN_SLOTS,
    Mode,
    PoolRow,
    check_lineup,
    lineup_key,
    slot_accepts,
)

pytestmark = pytest.mark.c0a


def _row(role_id, person_key, name, team, position, roster_positions, salary):
    return PoolRow(
        role_id=role_id,
        person_key=person_key,
        name=name,
        team=team,
        position=position,
        roster_positions=frozenset(roster_positions),
        salary=salary,
        game_info="",
        appg_raw=5.0,
        appg_flag="VALUE",
    )


def _classic_row(i, team, position, roster_positions, salary=3000):
    return _row(f"role{i}", f"p{i}|{team}|{position}", f"Player {i}", team, position, roster_positions, salary)


def _legal_classic_rows():
    # 2 C, 3 W, 2 D, 1 skater UTIL, 1 G, spanning 3 teams among skaters.
    return [
        _classic_row(1, "TOR", "C", {"C"}),
        _classic_row(2, "TOR", "C", {"C"}),
        _classic_row(3, "BOS", "W", {"W"}),
        _classic_row(4, "BOS", "W", {"W"}),
        _classic_row(5, "NYR", "W", {"W"}),
        _classic_row(6, "TOR", "D", {"D"}),
        _classic_row(7, "BOS", "D", {"D"}),
        _classic_row(8, "NYR", "W", {"W", "UTIL"}),
        _classic_row(9, "NYR", "G", {"G"}, salary=4000),
    ]


def test_classic_legal_lineup_passes():
    result = check_lineup(_legal_classic_rows(), Mode.CLASSIC)
    assert result.ok, result.reasons


def test_classic_two_team_eight_skater_roster_fails():
    rows = _legal_classic_rows()
    # Collapse both NYR skaters (index 4 and index 7) onto BOS: skaters now span only TOR/BOS.
    rows[4] = _classic_row(5, "BOS", "W", {"W"})
    rows[7] = _classic_row(8, "BOS", "W", {"W", "UTIL"})
    result = check_lineup(rows, Mode.CLASSIC)
    assert not result.ok
    assert any("team" in r for r in result.reasons)


def test_classic_goalie_does_not_count_toward_three_teams():
    rows = _legal_classic_rows()
    # Skaters collapsed to 2 teams (TOR, BOS); goalie placed on a 3rd team (CHI) must not rescue the count.
    rows[4] = _classic_row(5, "BOS", "W", {"W"})
    rows[7] = _classic_row(8, "BOS", "W", {"W", "UTIL"})
    rows[8] = _classic_row(9, "CHI", "G", {"G"}, salary=4000)
    result = check_lineup(rows, Mode.CLASSIC)
    assert not result.ok
    assert any("skaters span 2 team" in r for r in result.reasons)


def test_classic_goalie_in_util_fails():
    rows = _legal_classic_rows()
    rows[7] = _classic_row(8, "NYR", "G", {"G"}, salary=4000)  # goalie role in UTIL slot
    result = check_lineup(rows, Mode.CLASSIC)
    assert not result.ok


def test_classic_salary_50001_fails():
    rows = _legal_classic_rows()
    current_total = sum(r.salary for r in rows)
    bumped_goalie_salary = rows[8].salary + (SALARY_CAP + 1 - current_total)
    rows[8] = _classic_row(9, "NYR", "G", {"G"}, salary=bumped_goalie_salary)
    total = sum(r.salary for r in rows)
    assert total == SALARY_CAP + 1
    result = check_lineup(rows, Mode.CLASSIC)
    assert not result.ok
    assert any("salary" in r for r in result.reasons)


def test_classic_duplicate_person_fails():
    rows = _legal_classic_rows()
    rows[1] = _row("role2", rows[0].person_key, "Player 1 Dup", rows[0].team, "C", {"C"}, 3000)
    result = check_lineup(rows, Mode.CLASSIC)
    assert not result.ok
    assert any("duplicate" in r for r in result.reasons)


def test_slot_accepts_util_only_for_skaters():
    skater = _classic_row(1, "TOR", "W", {"W", "UTIL"})
    goalie = _classic_row(2, "TOR", "G", {"G"})
    assert slot_accepts("UTIL", skater, Mode.CLASSIC)
    assert not slot_accepts("UTIL", goalie, Mode.CLASSIC)


def test_slot_accepts_g_only_for_goalie():
    goalie = _classic_row(1, "TOR", "G", {"G"})
    skater = _classic_row(2, "TOR", "W", {"W"})
    assert slot_accepts("G", goalie, Mode.CLASSIC)
    assert not slot_accepts("G", skater, Mode.CLASSIC)


def _showdown_row(i, team, roster_positions, salary=3000):
    return _row(f"srole{i}", f"sp{i}|{team}", f"Showdown Player {i}", team, "C", roster_positions, salary)


def _legal_showdown_rows():
    return [
        _showdown_row(1, "TOR", {"CPT"}, salary=6000),
        _showdown_row(2, "TOR", {"FLEX"}),
        _showdown_row(3, "TOR", {"FLEX"}),
        _showdown_row(4, "BOS", {"FLEX"}),
        _showdown_row(5, "BOS", {"FLEX"}),
        _showdown_row(6, "BOS", {"FLEX"}),
    ]


def test_showdown_legal_lineup_passes():
    result = check_lineup(_legal_showdown_rows(), Mode.SHOWDOWN)
    assert result.ok, result.reasons


def test_showdown_flex_row_in_cpt_slot_fails():
    rows = _legal_showdown_rows()
    rows[0] = _showdown_row(1, "TOR", {"FLEX"}, salary=6000)
    result = check_lineup(rows, Mode.SHOWDOWN)
    assert not result.ok


def test_showdown_one_team_roster_fails():
    rows = _legal_showdown_rows()
    rows[3] = _showdown_row(4, "TOR", {"FLEX"})
    rows[4] = _showdown_row(5, "TOR", {"FLEX"})
    rows[5] = _showdown_row(6, "TOR", {"FLEX"})
    result = check_lineup(rows, Mode.SHOWDOWN)
    assert not result.ok
    assert any("team" in r for r in result.reasons)


def test_showdown_two_opposing_goalies_pass():
    rows = _legal_showdown_rows()
    rows[0] = _row("srole1", "gk1|TOR", "Goalie One", "TOR", "G", {"CPT"}, 6000)
    rows[1] = _row("srole2", "gk2|BOS", "Goalie Two", "BOS", "G", {"FLEX"}, 3000)
    result = check_lineup(rows, Mode.SHOWDOWN)
    assert result.ok, result.reasons


def test_showdown_four_team_pool_two_teams_represented_passes():
    # Pool has players from 4 possible teams but this lineup only uses 2 of them; that's legal
    # for Showdown (which only requires >=2 teams among the six selected, not among the pool).
    rows = _legal_showdown_rows()
    result = check_lineup(rows, Mode.SHOWDOWN)
    assert result.ok, result.reasons


def test_lineup_key_ignores_flex_order_and_distinguishes_captains():
    rows_a = _legal_showdown_rows()
    rows_b = _legal_showdown_rows()
    # Swap two FLEX rows: same set, different order.
    rows_b[1], rows_b[2] = rows_b[2], rows_b[1]
    assert lineup_key(rows_a, Mode.SHOWDOWN) == lineup_key(rows_b, Mode.SHOWDOWN)

    rows_c = _legal_showdown_rows()
    # Different captain (rows_c[0] and rows_c[1] swap identities via person_key), key must differ.
    rows_c[0], rows_c[1] = (
        _row("srole1", rows_c[1].person_key, rows_c[1].name, rows_c[1].team, "C", {"CPT"}, 6000),
        _row("srole2", rows_c[0].person_key, rows_c[0].name, rows_c[0].team, "C", {"FLEX"}, 3000),
    )
    assert lineup_key(rows_a, Mode.SHOWDOWN) != lineup_key(rows_c, Mode.SHOWDOWN)


def test_classic_slots_and_showdown_slots_shapes():
    assert CLASSIC_SLOTS.count("C") == 2
    assert CLASSIC_SLOTS.count("W") == 3
    assert CLASSIC_SLOTS.count("D") == 2
    assert CLASSIC_SLOTS.count("UTIL") == 1
    assert CLASSIC_SLOTS.count("G") == 1
    assert SHOWDOWN_SLOTS.count("CPT") == 1
    assert SHOWDOWN_SLOTS.count("FLEX") == 5
