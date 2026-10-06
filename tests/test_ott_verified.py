"""C17 (B85): Ottawa's DraftKings team code is verified from the DK lobby GameSets, so its games price on the odds feed."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.intake.salary import GameInfo
from nhl_dfs.models import field_inputs, ownership
from pool_builder import varied_pool

pytestmark = pytest.mark.c17


@pytest.fixture(scope="module")
def cfg():
    return ownership.load_ownership_config()


def test_ottawa_is_a_verified_dk_code_and_its_game_prices_on_market_and_reaches_the_field_b85(cfg):
    """OTT @ TOR ("Ottawa Senators @ Toronto Maple Leafs") is in the 2026-10-03 DK lobby GameSets, the first DraftKings file to show
    Ottawa. With the code verified, the partner feed's OTT @ TOR game matches, fits as MARKET and reaches the field features."""
    import yaml

    from conftest import fixture_bytes
    from nhl_dfs.data.sources import nhl
    from nhl_dfs.sim import market

    seen: dict[str, set[str]] = {}
    for gs in json.loads(fixture_bytes("dk_lobby_gamesets_2026-10-03.json"))["GameSets"]:
        for c in gs["Competitions"]:
            if c.get("Sport") == "NHL":
                away, home = (x.strip() for x in c["Description"].split("@"))
                seen.setdefault(away, set()).add(c["AwayTeamName"])
                seen.setdefault(home, set()).add(c["HomeTeamName"])
    assert seen["OTT"] == {"Senators"} and seen["TOR"] == {"Maple Leafs"}
    teams = {t["nhl"]: t for t in yaml.safe_load((market.REPO_ROOT / "config" / "teams.yaml").read_text(encoding="utf-8"))["teams"]}
    assert (teams["OTT"]["dk"], teams["OTT"]["dk_verified"]) == ("OTT", True) and teams["OTT"]["name"].endswith("Senators")
    snap = nhl.parse_partner_odds(json.loads(fixture_bytes("nhl_partner_odds_2026-10-03_ott.json")))
    got, why = market.match_odds(snap, {"OTT@TOR": ("TOR", "OTT")})
    assert why == {} and got["OTT@TOR"].total_line == 6.5
    gr = market.fit_game(got["OTT@TOR"], market.TeamStrength(3.0, 3.0), snap.as_of_utc, None, cfg=market.load_sim_config())
    assert gr.source == "MARKET"
    p = varied_pool(Mode.CLASSIC, teams=("OTT", "TOR", "COL", "LAK"))
    p = replace(p, games={"OTT@TOR": GameInfo("TOR", "OTT", "7:00PM ET", snap.as_of_utc + timedelta(hours=3))})
    inputs = field_inputs.collect(p, snapshot=snap, now=snap.as_of_utc + timedelta(hours=2), own_cfg=cfg)
    assert inputs.coverage["odds"]["games"] == 1 and [(g.home_abbrev, g.away_abbrev) for g in inputs.odds.games] == [("TOR", "OTT")]
