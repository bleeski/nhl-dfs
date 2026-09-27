import pytest

from nhl_dfs.contracts.scoring import (
    GoalieLine,
    SkaterLine,
    captain_twentieths,
    lineup_twentieths,
    score_goalie_tenths,
    score_skater_tenths,
    tenths_to_points,
)

pytestmark = pytest.mark.c0a


def test_goal_assist_sog_block_base_values():
    assert score_skater_tenths(SkaterLine(goals=1)) == 85
    assert score_skater_tenths(SkaterLine(assists=1)) == 50
    assert score_skater_tenths(SkaterLine(sog=1)) == 15
    assert score_skater_tenths(SkaterLine(blocks=1)) == 13


def test_sh_point_bonus_and_shootout_goal():
    assert score_skater_tenths(SkaterLine(sh_points=1)) == 20
    assert score_skater_tenths(SkaterLine(shootout_goals=1)) == 15
    # A shootout goal is not a real goal: it earns 15, not 85 + 15.
    assert score_skater_tenths(SkaterLine(shootout_goals=1)) != score_skater_tenths(SkaterLine(goals=1))


def test_hat_trick_boundary_2_vs_3_goals():
    two_goals = score_skater_tenths(SkaterLine(goals=2))
    three_goals = score_skater_tenths(SkaterLine(goals=3))
    assert two_goals == 2 * 85
    # 3 goals also crosses the 3+ points threshold (points = goals + assists), so both fire.
    assert three_goals == 3 * 85 + 30 + 30


def test_five_sog_boundary_4_vs_5():
    four = score_skater_tenths(SkaterLine(sog=4))
    five = score_skater_tenths(SkaterLine(sog=5))
    assert four == 4 * 15
    assert five == 5 * 15 + 30


def test_three_blocks_boundary_2_vs_3():
    two = score_skater_tenths(SkaterLine(blocks=2))
    three = score_skater_tenths(SkaterLine(blocks=3))
    assert two == 2 * 13
    assert three == 3 * 13 + 30


def test_three_points_boundary_2_vs_3():
    # points = goals + assists
    two_points = score_skater_tenths(SkaterLine(goals=1, assists=1))
    three_points = score_skater_tenths(SkaterLine(goals=2, assists=1))
    assert two_points == 85 + 50
    assert three_points == 2 * 85 + 50 + 30  # hat trick not yet (2 goals), but 3+ points fires


def test_bonuses_stack_three_goals_zero_assists():
    # 3 goals + 0 assists = 255 (goals) + 30 (hat trick) + 30 (3+ points) = 315
    line = SkaterLine(goals=3)
    assert score_skater_tenths(line) == 3 * 85 + 30 + 30


def test_sh_points_stack_two_sh_events():
    line = SkaterLine(goals=1, sh_points=2)
    assert score_skater_tenths(line) == 85 + 2 * 20


def test_goalie_win_loss_ot_loss_no_decision():
    assert score_goalie_tenths(GoalieLine(decision="W")) == 60
    assert score_goalie_tenths(GoalieLine(decision="L")) == 0
    assert score_goalie_tenths(GoalieLine(decision="OTL")) == 20
    assert score_goalie_tenths(GoalieLine(decision="ND")) == 0


def test_goalie_invalid_decision_raises():
    with pytest.raises(ValueError):
        GoalieLine(decision="TIE")


def test_goalie_25_saves_3_ga_loss():
    # 25 saves * 7 - 3 GA * 35 = 175 - 105 = 70
    line = GoalieLine(decision="L", saves=25, goals_against=3)
    assert score_goalie_tenths(line) == 70


def test_goalie_shootout_loss_zero_ga_is_shutout_plus_otl():
    # decision OTL + shutout True + 0 GA: 20 + 40 + saves*7
    line = GoalieLine(decision="OTL", saves=30, goals_against=0, shutout=True)
    assert score_goalie_tenths(line) == 20 + 40 + 30 * 7


def test_goalie_shutout_requires_full_game_caller_decides():
    # scoring.py does not infer shutout; caller sets the flag.
    line = GoalieLine(decision="W", saves=20, goals_against=0, shutout=True)
    assert score_goalie_tenths(line) == 60 + 40 + 20 * 7


def test_goalie_35_plus_saves_boundary():
    thirty_four = GoalieLine(decision="L", saves=34)
    thirty_five = GoalieLine(decision="L", saves=35)
    assert score_goalie_tenths(thirty_four) == 34 * 7
    assert score_goalie_tenths(thirty_five) == 35 * 7 + 30


def test_goalie_assist_earns_50():
    line = GoalieLine(decision="ND", assists=1)
    assert score_goalie_tenths(line) == 50


def test_goalie_offensive_bonuses_apply_like_a_skater():
    line = GoalieLine(decision="ND", goals=3)
    assert score_goalie_tenths(line) == 3 * 85 + 30 + 30  # hat trick + 3-points


def test_captain_multiplier_never_floats():
    # 13 tenths (1.3 pts) -> 39 twentieths (1.95 pts), exact integer arithmetic.
    assert captain_twentieths(13) == 39
    assert isinstance(captain_twentieths(13), int)


def test_lineup_twentieths_no_captain():
    base = [85, 50, 15]
    assert lineup_twentieths(base, None) == sum(t * 2 for t in base)


def test_lineup_twentieths_with_captain():
    base = [85, 50, 15]
    result = lineup_twentieths(base, captain_index=0)
    assert result == 85 * 3 + 50 * 2 + 15 * 2


def test_tenths_to_points():
    assert tenths_to_points(85) == 8.5
    assert tenths_to_points(0) == 0.0
    assert tenths_to_points(-35) == -3.5
