import copy
import json
from datetime import datetime, timezone

import pytest
import yaml
from conftest import REPO_ROOT, fixture_bytes, real_pair

from nhl_dfs.data.http import SourceSchemaError, load_sources_config
from nhl_dfs.data.sources import nhl

pytestmark = pytest.mark.c1


def _json(name):
    return json.loads(fixture_bytes(name))


def test_schedule_parses_and_filters_by_date():
    data = _json("nhl_schedule_2026-09-29.json")
    games = nhl.parse_schedule(data, on_date="2026-09-29")
    assert len(games) == 5
    first = games[0]
    assert (first.away, first.home, first.game_id) == ("FLA", "CAR", 2026020001)
    assert first.start_utc == datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    assert nhl.parse_schedule(data, on_date="2026-09-30") == []


def test_schedule_adapter_through_cache(make_cache):
    cache, transport = make_cache([("v1/schedule/2026-09-29", 200, fixture_bytes("nhl_schedule_2026-09-29.json"))])
    assert len(nhl.schedule("2026-09-29", cache=cache)) == 5
    assert transport.calls == ["https://api-web.nhle.com/v1/schedule/2026-09-29"]


def test_roster_parses_all_groups():
    players = nhl.parse_roster(_json("nhl_roster_VGK.json"), "VGK")
    assert len(players) == 16 + 9 + len(_json("nhl_roster_VGK.json")["goalies"])
    assert {p.position for p in players} <= {"C", "L", "R", "D", "G"}
    assert all(p.team == "VGK" and p.nhl_id > 0 for p in players)


def test_boxscore_parses_skaters_goalies_and_toi_seconds():
    box = nhl.parse_boxscore(_json("nhl_boxscore_2026010053.json"))
    assert box.game_id == 2026010053 and box.game_state in ("OFF", "FINAL")
    assert box.skaters and box.goalies
    forsberg = next(s for s in box.skaters if s.nhl_id == 8476887)
    assert (forsberg.sog, forsberg.toi_s) == (3, 19 * 60 + 12)
    assert all(g.toi_s >= 0 for g in box.goalies)


def test_game_log_parses():
    rows = nhl.parse_game_log(_json("nhl_gamelog_8478403_20252026_2.json"))
    assert len(rows) == 74 and all(r.toi_s > 0 for r in rows)


@pytest.mark.parametrize(
    "report, fields",
    [
        ("timeonice", ("evTimeOnIce", "ppTimeOnIce", "shTimeOnIce")),
        ("realtime", ("blockedShots",)),
        ("summary", ("goals", "assists", "shots", "ppPoints", "shPoints")),
    ],
)
def test_per_game_reports_parse(report, fields):
    rows, total = nhl.parse_report_page(_json(f"nhl_report_{report}.json"), report)
    assert total == 108 and len(rows) == 3
    for row in rows:
        for f in fields:
            assert isinstance(row[f], int), (f, row[f])


def test_report_pagination_reaches_total(make_cache):
    recorded = _json("nhl_report_timeonice.json")
    pages = {0: recorded["data"][:2], 2: recorded["data"][2:]}

    def serve(url):
        start = int(url.split("start=")[1].split("&")[0])
        return json.dumps({"data": pages[start], "total": 3}).encode()

    cfg = copy.deepcopy(load_sources_config())
    cfg["report_page_limit"] = 2
    cache, transport = make_cache([("skater/timeonice", 200, serve)], config=cfg)
    rows = nhl.skater_report("timeonice", "2026-04-01", "2026-04-01", cache=cache)
    assert [r["playerId"] for r in rows] == [r["playerId"] for r in recorded["data"]]
    assert len(transport.calls) == 2 and "start=2&limit=2" in transport.calls[1]
    assert "gameDate%3E%3D%222026-04-01%22" in transport.calls[0]


def test_report_missing_field_is_schema_error():
    data = _json("nhl_report_timeonice.json")
    del data["data"][1]["shTimeOnIce"]
    with pytest.raises(SourceSchemaError):
        nhl.parse_report_page(data, "timeonice")


def test_partner_odds_parse_two_way_three_way_total_and_puck():
    snap = nhl.parse_partner_odds(_json("nhl_partner_odds.json"))
    assert snap.book == "DraftKings" and snap.as_of_basis == "source"
    assert snap.as_of_utc == datetime(2026, 8, 28, 18, 0, 38, tzinfo=timezone.utc)  # kept even though stale
    g = next(x for x in snap.games if x.game_id == "2026020001")
    assert (g.away_abbrev, g.home_abbrev) == ("FLA", "CAR")
    assert (g.home_ml, g.away_ml) == (-125, 105)
    assert (g.home_ml_3way, g.away_ml_3way, g.draw_ml) == (130, 145, 320)
    assert (g.total_line, g.over_price, g.under_price) == (6.5, 105, -125)
    assert g.home_puck == (-1.5, 190) and g.away_puck == (1.5, -230)


def test_partner_odds_inconsistent_draw_fails_closed():
    data = _json("nhl_partner_odds.json")
    for o in data["games"][0]["awayTeam"]["odds"]:
        if o["description"] == "MONEY_LINE_3_WAY" and o["qualifier"] == "Draw":
            o["value"] = 999.0
    with pytest.raises(SourceSchemaError, match="draw"):
        nhl.parse_partner_odds(data)


def test_teams_config_has_32_observed_rows():
    teams = yaml.safe_load((REPO_ROOT / "config" / "teams.yaml").read_text(encoding="utf-8"))["teams"]
    assert len(teams) == 32
    assert len({t["nhl"] for t in teams}) == 32 and "UTA" in {t["nhl"] for t in teams}
    assert len({t["df_slug"] for t in teams}) == 32
    for t in teams:
        assert set(t) == {"nhl", "dk", "dk_verified", "covers", "df_slug", "name"}
        assert (t["dk"] is not None) == t["dk_verified"]
    schedule_teams = {x for g in nhl.parse_schedule(_json("nhl_schedule_2026-09-29.json")) for x in (g.away, g.home)}
    assert schedule_teams <= {t["nhl"] for t in teams}


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_every_real_csv_team_maps_to_a_verified_dk_row(mode):
    import csv
    import io

    sal, _ = real_pair(mode, "2026-09-29")  # the slate teams.yaml's DK codes were observed from
    teams = yaml.safe_load((REPO_ROOT / "config" / "teams.yaml").read_text(encoding="utf-8"))["teams"]
    dk = {t["dk"] for t in teams if t["dk_verified"]}
    rows = list(csv.DictReader(io.StringIO(sal.read_bytes().decode("utf-8-sig"))))
    assert {r["TeamAbbrev"] for r in rows} <= dk
