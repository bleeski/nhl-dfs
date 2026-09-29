"""C7: the Daily Faceoff adapter, on payloads trimmed from the 2026-09-29 captures."""

import json
import re
from datetime import date, datetime, timezone

import pytest

from conftest import HTTP
from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.http import SourceSchemaError
from nhl_dfs.data.sources import dailyfaceoff as df

pytestmark = pytest.mark.c7

TEAM_HTML = (HTTP / "dailyfaceoff_team_vancouver-canucks.html").read_text(encoding="utf-8")
GOALIE_HTML = (HTTP / "dailyfaceoff_goalies.html").read_text(encoding="utf-8")
DAY = date(2026, 9, 29)


def _without_payload(html: str) -> str:
    return re.sub(r'<script id="__NEXT_DATA__".*?</script>', "", html, flags=re.S)


def _edit_payload(html: str, fn) -> str:
    m = re.search(r'(<script id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', html, re.S)
    data = json.loads(m.group(2))
    fn(data["props"]["pageProps"])
    return html[:m.start(2)] + json.dumps(data) + html[m.end(2):]


def test_team_page_parses_lines_pairs_units_goalies_injuries_and_the_timestamp():
    t = df.parse_team_page(TEAM_HTML, "vancouver-canucks")
    assert t.team == "VAN" and t.team_name == "Vancouver Canucks"
    assert t.updated_utc == datetime(2026, 9, 28, 18, 14, 15, 527000, tzinfo=timezone.utc)  # updatedAt, not fetch time
    assert t.source_name and t.source_url.startswith("https://")
    assert [len(x) for x in t.f_lines] == [3, 3, 3, 3] and [len(x) for x in t.d_pairs] == [2, 2, 2]
    assert len(t.pp1) == 5 and len(t.pp2) == 5 and len(t.pk1) == 4 and len(t.pk2) == 4
    assert [g.name for g in t.goalies] == ["Kevin Lankinen", "Leevi Merilainen"]  # depth order
    assert dict(t.injuries) == {"Thatcher Demko": "out", "Filip Chytil": "ir", "Jake DeBrusk": "gtd"}
    # one person, one entry, with his real position even when listed on a power-play unit (PP entries say sk1..sk5)
    names = [p.name for p in t.players.values()]
    assert len(names) == len(set(names))
    assert all(not p.position.startswith("sk") for p in t.players.values())
    assert {p.position for p in t.pp1} <= {"c", "lw", "rw", "ld", "rd", "d"}
    deb = next(p for p in t.f_lines[0] if p.name == "Jake DeBrusk")
    assert deb.game_time_decision and deb.injury_status is None
    assert len(t.lineup()) == 18


def test_team_page_without_its_payload_fails_closed():
    with pytest.raises(SourceSchemaError):
        df.parse_team_page(_without_payload(TEAM_HTML), "vancouver-canucks")
    with pytest.raises(SourceSchemaError):
        df.parse_team_page(_edit_payload(TEAM_HTML, lambda pp: pp.pop("combinations")), "vancouver-canucks")


def test_goalie_page_next_data_path_reads_state_source_and_game_date():
    reports = df.parse_goalie_page(GOALIE_HTML)
    assert len(reports) == 10 and {r.path for r in reports} == {"next_data"}
    jarry = next(r for r in reports if r.goalie_name == "Tristan Jarry")
    assert jarry.state is GoalieState.CONFIRMED and jarry.team == "EDM" and jarry.opponent == "VAN"
    assert jarry.source_name == "Bob Stauffer" and jarry.source_url.startswith("https://x.com/")
    assert jarry.news_created_utc == datetime(2026, 9, 28, 17, 42, 37, 590000, tzinfo=timezone.utc)
    assert jarry.game_date == DAY and jarry.game_utc == datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)  # the game's own time
    others = [r for r in reports if r.goalie_name != "Tristan Jarry"]
    assert all(r.state is GoalieState.EXPECTED and r.strength_raw is None for r in others)


def test_only_the_exact_confirmed_strength_confirms_and_an_unknown_strength_is_reported():
    def edit(pp):
        pp["data"][0]["homeNewsStrengthName"] = "Likely"
        pp["data"][1]["homeNewsStrengthName"] = "confirmed"  # wrong case is not the recognized value
    reports = df.parse_goalie_page(_edit_payload(GOALIE_HTML, edit))
    car = next(r for r in reports if r.team == "CAR")
    tor = next(r for r in reports if r.team == "TOR")
    assert car.state is GoalieState.EXPECTED and car.strength_raw == "Likely" and not car.strength_recognized
    assert tor.state is GoalieState.EXPECTED and not tor.strength_recognized


def _routes(goalie_body, team_body):
    return [("starting-goalies", 200, goalie_body.encode()), ("line-combinations", 200, team_body.encode())]


def test_fetch_takes_the_next_data_path_and_logs_it(make_cache, caplog):
    cache, transport = make_cache(_routes(GOALIE_HTML, TEAM_HTML))
    with caplog.at_level("INFO"):
        got = df.fetch_goalies(DAY, cache=cache)
    assert got.path == "next_data" and len(got.reports) == 10
    assert any("path next_data" in r.message for r in caplog.records)
    assert not any("line-combinations" in u for u in transport.calls)  # no team page needed


def test_fetch_falls_to_team_pages_when_the_tag_is_removed(make_cache):
    cache, transport = make_cache(_routes(_without_payload(GOALIE_HTML), TEAM_HTML))
    got = df.fetch_goalies(DAY, cache=cache, teams=["VAN", "EDM"])
    assert got.path == "team_pages" and sorted(r.team for r in got.reports) == ["EDM", "VAN"]
    assert all(r.state is GoalieState.EXPECTED and r.game_date is None and r.path == "team_pages" for r in got.reports)
    assert any("depth order" in n for n in got.notes)


def test_fetch_returns_nothing_when_both_paths_fail(make_cache):
    cache, _ = make_cache(_routes(_without_payload(GOALIE_HTML), _without_payload(TEAM_HTML)))
    got = df.fetch_goalies(DAY, cache=cache, teams=["VAN"])
    assert got.reports == [] and got.path == "none"
    assert df.starting_goalies(DAY, cache=cache, teams=["VAN"]) == []


def test_a_fetched_old_confirmation_never_changes_its_game_date(make_cache):
    """The page holds games dated 2026-09-29. Asked for the 30th it does not re-date them."""
    cache, _ = make_cache(_routes(GOALIE_HTML, _without_payload(TEAM_HTML)))
    got = df.fetch_goalies(date(2026, 9, 30), cache=cache, teams=["VAN"])
    assert got.reports == [] and got.path == "none"
    assert any("no game dated 2026-09-30" in n for n in got.notes)
    parsed = df.parse_goalie_page(GOALIE_HTML)
    assert {r.game_date for r in parsed} == {DAY}


def test_capture_records_the_goalie_path_and_never_lets_a_parse_problem_stop_it(tmp_path):
    import sys

    from conftest import REPO_ROOT

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import capture

    items = [{"name": "df_starting_goalies", "ok": True}]
    capture._record_goalie_path(items, tmp_path, GOALIE_HTML)
    e = items[0]
    assert e["goalie_path"] == "next_data" and e["goalie_reports"] == 10 and e["goalie_confirmed"] == 1
    assert e["goalie_game_dates"] == ["2026-09-29"]
    rows = json.loads((tmp_path / "df_starting_goalies.parsed.json").read_text(encoding="utf-8"))
    assert {r["goalie"] for r in rows if r["state"] == "CONFIRMED"} == {"Tristan Jarry"}
    broken = [{"name": "df_starting_goalies", "ok": True}]
    capture._record_goalie_path(broken, tmp_path, _without_payload(GOALIE_HTML))  # does not raise
    assert broken[0]["goalie_path"] == "none" and "no __NEXT_DATA__" in broken[0]["goalie_path_error"]
    failed = [{"name": "df_starting_goalies", "ok": False}]
    capture._record_goalie_path(failed, tmp_path, None)
    assert "goalie_path" not in failed[0]
