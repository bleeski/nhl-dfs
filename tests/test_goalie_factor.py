"""C20: goalie as the top-1% factor. B37 (unresolved goalie pairs in the research request), the own-goalie rule
(flags 51 to 53) and the goalie leverage term (flag 54). No network in any test."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import TESTS
from nhl_dfs.build import packet
from nhl_dfs.build.run import run_slate

pytestmark = pytest.mark.c20

LS = TESTS / "fixtures" / "late_swap" / "classic"  # 6 teams in 3 games, 2 goalies per team, real Game Info
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # all three games are ahead
AFTER_FIRST = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # AAA@BBB (7:00PM ET) has started
SLATE_TEAMS = {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}


@pytest.fixture(scope="module")
def run0(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c20_b37")
    r = run_slate(LS / "DKSalaries.csv", LS / "DKEntries.template.csv", offline=True, baseline_only=True,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs", clock=lambda: BEFORE)
    assert r.ok, r.manifest["failed"]
    return r


def _portfolio_goalie_teams(run) -> set[str]:
    v = packet.RunView(run)
    return {v.row(r).team for lu in v.lineups.values() for r in lu if r and v.row(r).is_goalie}


def _unresolved(req) -> list[dict]:
    return [p for p in req["players"] if p["why"] == packet.UNRESOLVED_WHY]


def test_b37_both_goalies_of_an_unresolved_team_outside_the_portfolio_are_listed(run0):
    in_portfolio = _portfolio_goalie_teams(run0.run)
    outside = SLATE_TEAMS - in_portfolio
    assert in_portfolio and outside  # the defect needs a team the lineups do not use
    req = packet.research_request(run0.run, now=BEFORE)
    got = {t: sorted(p["name"] for p in _unresolved(req) if p["team"] == t) for t in outside}
    assert all(len(names) == 2 for names in got.values()), got  # BOTH goalies of each such team
    assert all(not any(p["team"] == t for p in _unresolved(req)) for t in in_portfolio)  # those are listed under today's reason
    assert set(req["unresolved_goalie_teams"]) == outside and req["unresolved_goalie_teams_not_listed"] == []
    for p in _unresolved(req):
        assert p["pos"] == "G" and p["role_id"] and p["entries"] == 0 and p["game_id"] and p["start_utc"]
        assert "current" in p  # the override's `old` value rides along when the role state exists
    # the research URLs now cover those teams' line pages (a slug is looked up per team code)
    assert len(req["urls"]) >= 1


def test_b37_exposed_players_stay_first_and_the_list_never_passes_max_players(run0):
    cfg = packet.load_qa_config()
    full = packet.research_request(run0.run, cfg, now=BEFORE)
    first_unresolved = next(i for i, p in enumerate(full["players"]) if p["why"] == packet.UNRESOLVED_WHY)
    assert all(p["why"] != packet.UNRESOLVED_WHY for p in full["players"][:first_unresolved])
    assert all(p["why"] == packet.UNRESOLVED_WHY for p in full["players"][first_unresolved:])
    starts = [p["start_utc"] for p in full["players"][first_unresolved:]]
    assert starts == sorted(starts)  # soonest game first
    n_tier0 = first_unresolved
    assert n_tier0 >= 1
    for cap in (n_tier0, n_tier0 + 1, n_tier0 + 3, 25):
        c = {**cfg, "research": {**cfg["research"], "max_players": cap}}
        req = packet.research_request(run0.run, c, now=BEFORE)
        assert len(req["players"]) <= cap
        listed_teams = {p["team"] for p in _unresolved(req)}
        assert all(sum(1 for p in _unresolved(req) if p["team"] == t) == 2 for t in listed_teams)  # a pair is never split
        assert set(req["unresolved_goalie_teams"]) == listed_teams
        # today's list is identical to the one without the new pairs: it was never displaced
        assert [p for p in req["players"] if p["why"] != packet.UNRESOLVED_WHY] == full["players"][:n_tier0]
    tight = {**cfg, "research": {**cfg["research"], "max_players": n_tier0 + 1}}
    req = packet.research_request(run0.run, tight, now=BEFORE)
    assert _unresolved(req) == [] and req["unresolved_goalie_teams_not_listed"]  # one slot left: no pair fits, and it is said


def test_b37_a_team_whose_starter_is_confirmed_adds_none(run0, monkeypatch):
    from nhl_dfs.contracts.statuses import GoalieState

    outside = sorted(SLATE_TEAMS - _portfolio_goalie_teams(run0.run))
    confirmed = outside[0]
    real = packet._role_state_now

    def fake(v, now):
        rs, note = real(v, now)
        goalies = dict(rs.goalies) if rs is not None else {}
        goalies[confirmed] = SimpleNamespace(state=GoalieState.CONFIRMED, confirmed="x", p_start={})
        return SimpleNamespace(goalies=goalies, persons=rs.persons if rs is not None else {}), note

    monkeypatch.setattr(packet, "_role_state_now", fake)
    req = packet.research_request(run0.run, now=BEFORE)
    assert confirmed not in {p["team"] for p in _unresolved(req)}
    assert set(outside) - {confirmed} == {p["team"] for p in _unresolved(req)}


def _with_starting(src, dst, name_fragment: str):
    out = []
    for line in src.read_bytes().split(b"\n"):
        body = line.rstrip(b"\r")
        if name_fragment.encode() in body and body.endswith(b",,"):
            line = body[:-1] + b"P" + line[len(body):]
        out.append(line)
    dst.write_bytes(b"\n".join(out))
    return dst


def test_b37_a_dk_starting_p_goalie_resolves_the_team(run0, tmp_path):
    outside = sorted(SLATE_TEAMS - _portfolio_goalie_teams(run0.run))
    team = outside[0]
    name = {"AAA": "Alpha", "BBB": "Bravo", "CCC": "Charlie", "DDD": "Delta", "EEE": "Echo", "FFF": "Foxtrot"}[team]
    sal = _with_starting(LS / "DKSalaries.csv", tmp_path / "DKSalaries.csv", f"{name} G1 (")
    r = run_slate(sal, LS / "DKEntries.template.csv", offline=True, baseline_only=True, out_root=tmp_path / "runs",
                  outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    assert r.ok, r.manifest["failed"]
    in_portfolio = _portfolio_goalie_teams(r.run)
    req = packet.research_request(r.run, now=BEFORE)
    teams = {p["team"] for p in _unresolved(req)}
    if team not in in_portfolio:  # DK names its starter: nothing to ask about that team
        assert team not in teams
    assert teams <= SLATE_TEAMS - in_portfolio - {team}


def test_b37_a_game_that_started_adds_no_goalies(run0):
    req = packet.research_request(run0.run, now=AFTER_FIRST)
    assert not any(p["team"] in {"AAA", "BBB"} for p in _unresolved(req))  # its rows are not selectable
    later = {"CCC", "DDD", "EEE", "FFF"} - _portfolio_goalie_teams(run0.run)
    assert later <= {p["team"] for p in _unresolved(req)} or req["unresolved_goalie_teams_not_listed"]


def test_b37_without_a_role_state_every_open_team_counts_as_unresolved(run0, monkeypatch):
    monkeypatch.setattr(packet, "_role_state_now", lambda v, now: (None, "role state unavailable (test)"))
    req = packet.research_request(run0.run, now=BEFORE)
    outside = SLATE_TEAMS - _portfolio_goalie_teams(run0.run)
    assert {p["team"] for p in _unresolved(req)} == outside
    assert all(p["current"] is None for p in req["players"]) and "unavailable" in req["state_note"]
