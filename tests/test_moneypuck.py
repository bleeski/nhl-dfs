import copy
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TESTS
from nhl_dfs.data.history import moneypuck as mp
from nhl_dfs.data.http import SourceUnavailable, load_sources_config

pytestmark = pytest.mark.c4

H = TESTS / "fixtures" / "history"
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_config_lists_only_data_page_urls_and_attribution():
    c = load_sources_config()["moneypuck"]
    assert c["enabled"] is True and "MoneyPuck.com" in c["attribution"]
    for kind, levels in c["listed"].items():
        for level, url in levels.items():
            assert url.startswith(("https://peter-tanner.com/moneypuck/downloads/", "https://moneypuck.com/moneypuck/playerData/"))


def test_unlisted_pairs_are_refused():
    with pytest.raises(mp.NotListed):
        mp.listed_url("shots", 2025, "season")
    with pytest.raises(mp.NotListed):
        mp.listed_url("referees", 2025, "game")
    with pytest.raises(mp.NotListed):
        mp.load("shots", 2025, "season", path=H / "mp_shots_2025.zip")
    assert mp.listed_url("skaters", 20252026, "game").endswith("/seasonPlayersSummary/skaters/2025.zip")


def test_skaters_game_pivot_wide():
    df = mp.load("skaters", 2025, "game", path=H / "mp_skaters_game_2025.zip")
    assert len(df) == 8 and df["game_id"].unique().tolist() == [2025020017]
    assert df["season"].unique().tolist() == [20252026]
    for c in ("mp_toi_s", "mp_toi_5on5_s", "mp_toi_5on4_s", "mp_toi_4on5_s", "mp_toi_other_s", "a1", "a2", "ixg",
              "hd_xg", "onice_xgf", "onice_xga", "sog", "blocks", "goals", "assists"):
        assert c in df.columns and df[c].notna().all(), c
    assert (df["assists"] == df["a1"] + df["a2"]).all()
    assert (df["mp_toi_5on5_s"] <= df["mp_toi_s"]).all()


@pytest.mark.parametrize("kind, level, name, rows", [
    ("goalies", "game", "mp_goalies_game_2025.zip", 2),
    ("lines", "game", "mp_lines_game_2025.zip", 34),
    ("teams", "game", "mp_all_teams_game.csv", 10),
    ("shots", "game", "mp_shots_2025.zip", 40),
    ("skaters", "season", "mp_skaters_season_2025.csv", 8),
    ("goalies", "season", "mp_goalies_season_2025.csv", 2),
])
def test_parser_per_kind(kind, level, name, rows):
    df = mp.load(kind, 2025, level, path=H / name)
    assert len(df) == rows
    assert (df["season"] == 20252026).all()
    if kind == "shots":
        assert set(df["game_id"]) == {2025020017} and df["situation"].str.match(r"^\d on \d$".replace(" ", "")).all()
    if kind == "lines":
        assert df["player_ids"].str.split("|").map(len).isin([2, 3]).all()
    if kind == "goalies" and level == "game":
        assert (df["xga"] > 0).all()


def test_shared_ice_is_a_share():
    si = mp.shared_ice(mp.load("lines", 2025, "game", path=H / "mp_lines_game_2025.zip"))
    assert len(si) and si["shared_ice"].between(0, 1).all()


def _zip(payload: bytes) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("2025.csv", payload)
    return b.getvalue()


def test_download_writes_raw_and_meta_then_reuses(tmp_path):
    calls = []
    body = (H / "mp_skaters_game_2025.zip").read_bytes()

    def transport(url, headers, timeout_s):
        calls.append(url)
        return 200, body, {}

    p = mp.download("skaters", 2025, "game", raw_root=tmp_path, transport=transport, clock=lambda: T0)
    assert p.read_bytes() == body and calls == [mp.listed_url("skaters", 2025, "game")]
    meta = json.loads(p.with_name(p.name + ".meta.json").read_text())
    assert meta["bytes"] == len(body) and len(meta["sha256"]) == 64
    mp.download("skaters", 2025, "game", raw_root=tmp_path, transport=transport, clock=lambda: T0 + timedelta(hours=1))
    assert len(calls) == 1  # fresh: reused
    mp.download("skaters", 2025, "game", raw_root=tmp_path, transport=transport, clock=lambda: T0 + timedelta(days=2))
    assert len(calls) == 2
    assert len(mp.load("skaters", 2025, "game", raw_root=tmp_path)) == 8


def test_download_rejects_bad_bodies_and_disabled(tmp_path):
    with pytest.raises(SourceUnavailable):
        mp.download("skaters", 2025, "game", raw_root=tmp_path, transport=lambda u, h, t: (200, b"<html>blocked</html>", {}))
    with pytest.raises(SourceUnavailable):
        mp.download("goalies", 2025, "game", raw_root=tmp_path, transport=lambda u, h, t: (403, b"", {}))
    cfg = copy.deepcopy(load_sources_config())
    cfg["moneypuck"]["enabled"] = False
    with pytest.raises(SourceUnavailable):
        mp.download("skaters", 2025, "game", cfg=cfg, raw_root=tmp_path, transport=lambda u, h, t: (200, _zip(b"x"), {}))
