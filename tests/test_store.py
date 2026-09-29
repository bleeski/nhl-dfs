from datetime import date

import pandas as pd
import pytest

from nhl_dfs.data import history
from nhl_dfs.data.history import store

pytestmark = pytest.mark.c4


def frame(season, n=5):
    return pd.DataFrame({
        "nhl_id": range(n), "game_id": [season // 10000 * 1_000_000 + 20001 + i for i in range(n)],
        "game_date": [date(season // 10000, 10, 10 + i) for i in range(n)], "toi_s": [900.5 + i for i in range(n)],
        "a1_missing": [1] * n, "team": ["VAN"] * n, "home": [True, False, True, False, True][:n],
    })


def test_parquet_round_trip(tmp_path):
    df = frame(20252026)
    p = store.write("skater_games", 20252026, df, root=tmp_path)
    assert p == tmp_path / "skater_games" / "20252026.parquet" and p.exists()
    back = store.read("skater_games", [20252026], root=tmp_path)
    pd.testing.assert_frame_equal(back, df)
    assert isinstance(back["game_date"].iloc[0], date)


def test_read_concatenates_seasons_and_skips_missing(tmp_path):
    store.write("skater_games", 20242025, frame(20242025, 3), root=tmp_path)
    store.write("skater_games", 20252026, frame(20252026, 4), root=tmp_path)
    both = store.read("skater_games", [20252026, 20242025, 20232024], root=tmp_path)
    assert len(both) == 7 and both["game_id"].is_monotonic_increasing
    assert store.seasons_present("skater_games", root=tmp_path) == [20242025, 20252026]
    assert store.read("nothing", [20252026], root=tmp_path).empty


def test_write_replaces_and_leaves_no_temp(tmp_path):
    store.write("k", 20252026, frame(20252026, 5), root=tmp_path)
    store.write("k", 20252026, frame(20252026, 2), root=tmp_path)
    assert len(store.read("k", [20252026], root=tmp_path)) == 2
    assert [p.name for p in (tmp_path / "k").iterdir()] == ["20252026.parquet"]
    with pytest.raises(ValueError):
        store.path_for("../escape", 20252026, root=tmp_path)


def test_season_keys_and_regimes():
    assert history.nhl_season(2025) == 20252026 and history.mp_year(20252026) == 2025
    assert history.season_of(date(2026, 9, 28)) == 20262027 and history.season_of(date(2026, 4, 1)) == 20252026
    assert [history.regime_of(g) for g in (2026010053, 2025020017, 2025030111, 2025040001)] == \
        ["preseason", "regular", "playoffs", "other"]
    # Preseason 2026-27: the in-progress season plus the two completed ones.
    assert history.history_seasons(date(2026, 9, 28), 2) == [20242025, 20252026, 20262027]
    assert history.history_seasons(date(2026, 6, 1), 2) == [20242025, 20252026]
