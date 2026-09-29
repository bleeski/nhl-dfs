from datetime import date

import pandas as pd
import pytest

from nhl_dfs.data.features import asof
from nhl_dfs.data.history import combine, store
from nhl_dfs.data.history import moneypuck as mp
from test_nhl_reports import H, MP_FILES, SEASON, run

pytestmark = pytest.mark.c4

AS_OF = date(2025, 10, 15)


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("asof")
    run(tmp, moneypuck=True, sub="a")
    run(tmp, moneypuck=False, sub="b")
    root = tmp / "a" / "store"
    plant(root, int(store.read("skater_games", [SEASON], root=root)["nhl_id"].iloc[0]))
    return root, tmp / "b" / "store"


def plant(root, pid):
    sk = store.read("skater_games", [SEASON], root=root)
    base = sk[sk["nhl_id"] == pid].iloc[[0]]
    future = base.assign(game_id=2025020300, game_date=date(2025, 11, 20), goals=9.0)
    same_day = base.assign(game_id=2025020150, game_date=AS_OF, goals=8.0)
    pre = base.assign(game_id=2025010050, game_date=date(2025, 9, 25), team="XXX")
    store.write("skater_games", SEASON, pd.concat([sk, future, same_day, pre], ignore_index=True), root=root)


def test_same_day_and_future_games_are_excluded(roots):
    root, _ = roots
    pid = int(store.read("skater_games", [SEASON], root=root)["nhl_id"].iloc[0])
    f = asof.frame(AS_OF, [pid], store_root=root)
    assert (f.skaters["game_date"] < AS_OF).all()
    assert set(f.skaters["game_id"]) == {2025010050, 2025020017}
    assert 9.0 not in f.skaters["goals"].tolist() and 8.0 not in f.skaters["goals"].tolist()
    assert f.seasons == [20232024, 20242025, 20252026]


def test_regime_and_player_signals(roots):
    root, _ = roots
    pid = int(store.read("skater_games", [SEASON], root=root)["nhl_id"].iloc[0])
    f = asof.frame(AS_OF, [pid], store_root=root)
    s = f.skaters.set_index("game_id")
    assert s.loc[2025010050, "regime"] == "preseason" and s.loc[2025020017, "regime"] == "regular"
    assert s.loc[2025020017, "games_before"] == 1 and s.loc[2025020017, "team_stint"] == 1  # XXX -> real team
    assert s.loc[2025020017, "days_rest"] == 14
    p = f.players.set_index("nhl_id").loc[pid]
    assert p["games"] == 1  # aggregates use regular-season games only


def test_missing_indicators_present_in_both_tiers(roots):
    a_root, b_root = roots
    fa, fb = asof.frame(AS_OF, None, store_root=a_root), asof.frame(AS_OF, None, store_root=b_root)
    for c in combine.SKATER_ENRICH:
        assert f"{c}_missing" in fa.skaters and f"{c}_missing" in fb.skaters
        assert f"{c}_missing_share" in fa.players and f"{c}_missing_share" in fb.players
    reg = fb.players
    assert (reg["a1_missing_share"] == 1.0).all() and (reg["ixg_missing_share"] == 1.0).all()
    assert (fa.players["ixg_missing_share"] == 0.0).all() and (fa.players["tier_a_share"] == 1.0).all()


def test_season_level_rows_of_the_current_season_never_leak(tmp_path):
    ss = mp.load("skaters", 2025, "season", path=H / "mp_skaters_season_2025.csv")  # the 2025-26 season summary
    store.write("mp_skater_seasons", 20252026, ss, root=tmp_path)
    prior = ss.assign(season=20242025)
    store.write("mp_skater_seasons", 20242025, prior, root=tmp_path)
    f = asof.frame(AS_OF, None, store_root=tmp_path)
    assert set(f.season_level["season"]) == {20242025}
    assert any("still in progress" in n for n in f.notes)
    after = asof.frame(date(2026, 9, 28), None, store_root=tmp_path)  # 2025-26 is complete by then
    assert set(after.season_level["season"]) == {20242025, 20252026}


def test_empty_store_is_reported_not_raised(tmp_path):
    f = asof.frame(AS_OF, [1], store_root=tmp_path)
    assert f.skaters.empty and f.players.empty and any("nothing stored" in n for n in f.notes)
