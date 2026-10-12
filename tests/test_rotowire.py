"""C54 (B121): RotoWire NHL news as a second goalie and scratch source. Every clock is pinned; no test goes online.

Statement and binding tests on short sentences are marked CONSTRUCTED: they test the reading rules (flags 86, 87) on
literal sentences and are never stored as a page. Board and late-swap tests inject RotoWire rows at
goalies.gather_rotowire and Daily Faceoff rows at goalies.gather, on the C2c late-swap fixture (synthetic teams AAA to
FFF, mapped to themselves in the team-code table for these tests only)."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import TESTS, FakeTransport
from nhl_dfs.build import goalies, late_swap, packet
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.statuses import GoalieState
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.data.sources import dailyfaceoff as df
from nhl_dfs.data.sources import rotowire as rw
from nhl_dfs.data.sources.dailyfaceoff import GoalieReport
from nhl_dfs.intake.entries import read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models import roles

pytestmark = pytest.mark.c54

LS = TESTS / "fixtures" / "late_swap"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
STARTS = {"AAA": G1, "BBB": G1, "CCC": datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc),
          "DDD": datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc), "EEE": datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc),
          "FFF": datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)}
OPP = {"AAA": "BBB", "BBB": "AAA", "CCC": "DDD", "DDD": "CCC", "EEE": "FFF", "FFF": "EEE"}
GAME_DAY = date(2026, 10, 15)  # every fixture game is on the New York date 2026-10-15 (a Thursday)
CITED = "https://www.example-team.test/news/starter"


# -- helpers ----------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_codes(monkeypatch):
    codes = {t.lower(): {"nhl": t, "dk": t, "name": t} for t in STARTS}
    monkeypatch.setattr(roles, "team_codes", lambda *a, **k: codes)
    monkeypatch.setattr(df, "team_codes", lambda *a, **k: codes)
    return codes


def df_report(team, name, *, created, state=GoalieState.CONFIRMED):
    return GoalieReport(team, None, name, None, state, state.value.title(), True, STARTS[team].date(), STARTS[team],
                        OPP[team], "test page", "https://www.dailyfaceoff.com/starting-goalies", created, "next_data")


def rw_report(team, name, *, at, state=GoalieState.CONFIRMED, cited=True):
    return GoalieReport(team, None, name, None, state, "will start", True, GAME_DAY, STARTS[team], OPP[team],
                        "Team site" if cited else rw.AGGREGATOR, CITED if cited else "https://www.rotowire.com/hockey/news.php",
                        at, "rotowire")


def news(reports=(), scratches=(), *, source="stored", fetched=None, notes=()):
    f = rw.NewsFetch(list(reports), list(scratches), {"today": len(reports) + len(scratches)}, list(notes), source,
                     fetched or BEFORE - timedelta(minutes=4))
    if source == "none":
        f.fetched_utc = None
    return f


def inject_df(monkeypatch, reports, *, path="next_data"):
    def fake(pool, cache, *, offline, budget_s, now):
        f = now - timedelta(minutes=3)
        page = {"source": "stored", "fetched_utc": goalies._iso(f), "age_min": 3.0} if path != "none" else \
            {"source": "none", "fetched_utc": None, "age_min": None}
        return {}, list(reports), path, ([] if path != "none" else ["goalie page unusable (SourceUnavailable)"]), page
    monkeypatch.setattr(goalies, "gather", fake)


def inject_rw(monkeypatch, fetch):
    monkeypatch.setattr(goalies, "gather_rotowire", lambda *a, **k: fetch)


def base(tmp_path):
    d = LS / "classic"
    r = run_slate(d / "DKSalaries.csv", d / "DKEntries.template.csv", offline=True, out_root=tmp_path / "runs",
                  clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def clean_current(tmp_path):
    """The C2c current export without 7100000001 (Echo C1, OUT in the salary file) and 7100000005 (blank)."""
    src = LS / "classic" / "DKEntries.current.csv"
    lines = src.read_bytes().split(b"\r\n")
    dst = tmp_path / "cur.csv"
    dst.write_bytes(b"\r\n".join(x for x in lines if not any(x.lstrip(b"\xef\xbb\xbf").startswith(e)
                                                              for e in (b"7100000001", b"7100000005"))))
    return dst


def swap(tmp_path, b, current, *, as_of=BEFORE):
    return late_swap.run(b.run.run_id, current, runs_root=tmp_path / "runs", as_of=as_of, offline=True)


def pool():
    return read_salary(LS / "classic" / "DKSalaries.csv")


def person(p, name):
    return next(r.person_key for r in p.rows if r.name == name)


def names_in(path, p, eid):
    return {p.by_role_id[x].name for x in goalies.lineups_of(path, p)[eid] if x}


# -- statements and game binding (flags 86, 87) -------------------------------------------------------------------

SAT = rw.SlateGame("CAR", "TBL", date(2026, 10, 10), datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc))  # a Saturday


def kind(news_text, headline="", player="Pyotr Kochetkov", game=SAT):
    return rw.read_item(rw.NewsItem(player, "CAR", "G", headline, news_text, None, None), game, rw.team_names())[0]


@pytest.mark.parametrize("text,expected", [  # CONSTRUCTED sentences
    ("Kochetkov will start Saturday against the Lightning, per the Hurricanes' official site.", "start"),
    ("Kochetkov is expected to start Saturday against the Lightning.", "expected"),
    ("Kochetkov is the probable starter Saturday against Tampa Bay.", "expected"),
    ("Kochetkov will likely start Saturday versus the Lightning.", "expected"),
    ("Kochetkov will not start Saturday against Tampa Bay.", "ignored_statement"),
    ("Kochetkov won't start Saturday.", "ignored_statement"),
    ("Kochetkov is a game-time decision for Saturday's game.", "ignored_statement"),
    ("Kochetkov is questionable for Saturday's game against the Lightning.", "ignored_statement"),
    ("Kochetkov will be a healthy scratch Saturday against the Lightning.", "scratch"),
    ("Kochetkov has been ruled out for Saturday's game.", "scratch"),
    ("Kochetkov, ruled out earlier, will play tonight.", "ignored_contradiction"),
])
def test_statement_rules(text, expected):
    assert kind(text) == expected


@pytest.mark.parametrize("text,expected", [  # CONSTRUCTED sentences
    ("Kochetkov will start Sunday against the Lightning.", "ignored_other_game"),
    ("Kochetkov will start Saturday against the Bruins.", "ignored_other_game"),
    ("Kochetkov will start Friday against the Panthers, while Andersen will get the nod Saturday versus the Lightning.",
     "ignored_other_game"),
    ("Kochetkov will start for AHL Chicago on Saturday.", "ignored_ahl"),
    ("Kochetkov will start.", "ignored_unbound"),
    ("With Kochetkov sidelined, Andersen will start Saturday.", "ignored_subject"),
    ("He'll be between the pipes Saturday in Tampa Bay.", "start"),
    ("Kochetkov, who allowed two goals Thursday, will start Saturday's game.", "start"),
    ("Kochetkov will start Saturday after Andersen was scratched.", "start"),
])
def test_a_statement_counts_only_for_the_slate_game_and_its_player(text, expected):
    assert kind(text) == expected


def test_a_back_to_back_note_binds_only_the_slate_games_clause():  # CONSTRUCTED
    text = "Kochetkov will start Friday against the Panthers, while Andersen will get the nod Saturday versus the Lightning."
    assert kind(text, player="Frederik Andersen") == "start"
    assert kind(text, player="Pyotr Kochetkov") == "ignored_other_game"


def test_the_headline_binds_a_statement_that_names_no_day():  # CONSTRUCTED
    assert kind("Kochetkov will start.", headline="Will start Saturday") == "start"
    assert kind("Kochetkov will start.", headline="Will start Sunday") == "ignored_other_game"


def _item(published, text="Kochetkov will start tonight against the Lightning.", team="CAR"):
    return rw.NewsItem("Pyotr Kochetkov", team, "G", "", text, published, "https://www.rotowire.com/hockey/news.php")


def test_the_new_york_day_decides_not_the_utc_date():
    # 2026-10-10T03:30Z is 23:30 on Friday 2026-10-09 in New York: yesterday's item for a Saturday game
    out = rw.read_items([_item(datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc))], {"CAR": SAT},
                        datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch())
    assert out.reports == [] and out.counts.get("skipped_other_day") == 1
    out = rw.read_items([_item(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc))], {"CAR": SAT},
                        datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch())
    (r,) = out.reports
    assert r.state is GoalieState.CONFIRMED and r.path == "rotowire" and r.game_date == SAT.day and r.opponent == "TBL"
    assert r.source_name == rw.AGGREGATOR  # no cited post on this item (flag 90)


def test_an_item_published_after_the_clock_is_ignored():
    out = rw.read_items([_item(datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc))], {"CAR": SAT},
                        datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch())
    assert out.reports == [] and out.counts.get("skipped_after_clock") == 1


def test_a_cited_post_is_the_rows_source():
    it = rw.NewsItem("Pyotr Kochetkov", "CAR", "G", "", "Kochetkov will start tonight against the Lightning.",
                     datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), "https://www.rotowire.com/hockey/news.php",
                     "Carolina Hurricanes", CITED)
    (r,) = rw.read_items([it], {"CAR": SAT}, datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch()).reports
    assert r.source_name == "Carolina Hurricanes" and r.source_url == CITED


# -- the fetch: rate, attempts, failures (flag 85) ------------------------------------------------------------------

def _cache(tmp_path, routes, *, offline=False, clock=None):
    t = FakeTransport(routes)
    c = HttpCache(tmp_path / "raw", config=load_sources_config(), transport=t, offline=offline,
                  clock=clock or (lambda: BEFORE), sleep=t.sleeps.append)
    return c, t


def test_a_403_is_unavailable_and_no_second_request_goes_out_inside_the_ttl(tmp_path):
    clock = {"t": BEFORE}
    c, t = _cache(tmp_path, [("rotowire.com", 403, b"Access Denied")], clock=lambda: clock["t"])
    first = rw.fetch_news({}, cache=c, now=BEFORE)
    assert first.source == "none" and "SourceUnavailable" in first.notes[0] and len(t.calls) == 1
    clock["t"] = BEFORE + timedelta(seconds=599)
    second = rw.fetch_news({}, cache=c, now=clock["t"])
    assert second.source == "none" and len(t.calls) == 1  # the stored view: no request inside 600 s of an attempt
    assert any("stored copy only" in n for n in second.notes)
    clock["t"] = BEFORE + timedelta(seconds=601)
    rw.fetch_news({}, cache=c, now=clock["t"])
    assert len(t.calls) == 2


def test_offline_with_nothing_cached_makes_no_request(tmp_path):
    c, t = _cache(tmp_path, [("rotowire.com", 200, b"")], offline=True)
    got = rw.fetch_news({}, cache=c, now=BEFORE)
    assert got.source == "none" and t.calls == [] and "offline and not cached" in got.notes[0]


# -- names to the pool (flag 88) -------------------------------------------------------------------------------------

def test_same_name_players_on_different_teams_map_by_team(monkeypatch):
    codes = {"aaa": {"nhl": "AAA", "dk": "AAA", "name": "A"}, "bbb": {"nhl": "BBB", "dk": "BBB", "name": "B"}}
    monkeypatch.setattr(df, "team_codes", lambda *a, **k: codes)
    rows = [SimpleNamespace(name="Sam Smith", team="AAA", person_key="sam smith|AAA", is_goalie=True),
            SimpleNamespace(name="Sam Smith", team="BBB", person_key="sam smith|BBB", is_goalie=False),
            SimpleNamespace(name="Pat Lee", team="AAA", person_key="pat lee|AAA", is_goalie=True)]
    fake_pool = SimpleNamespace(rows=rows)
    at = BEFORE - timedelta(hours=1)
    f = news([rw_report("AAA", "Sam Smith", at=at)],
             [rw.Scratch("Sam Smith", "BBB", "Smith will be scratched Thursday", rw.AGGREGATOR, None, at),
              rw.Scratch("Nobody Here", "AAA", "Here will not play Thursday", rw.AGGREGATOR, None, at)])
    starts, _, scratches, unmatched, _ = goalies._match_rotowire(f, fake_pool)
    assert starts["AAA"][0][0] == "sam smith|AAA" and set(scratches) == {"sam smith|BBB"}
    assert unmatched == ["Nobody Here (AAA): not in the pool"]


# -- the board and late swap (flag 89) ---------------------------------------------------------------------------------

def test_a_rotowire_confirmation_alone_confirms_with_its_source_and_time_and_repairs(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    at = BEFORE - timedelta(hours=1)
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=at)]))
    r = swap(tmp_path, b, cur)
    assert r.statuses["FILE_VALID"] == "TRUE"
    g = r.manifest["goalies"]
    d1 = g["persons"][person(p, "Delta G1")]
    assert d1["status"] == "CONFIRMED" and d1["source"].startswith("RotoWire (citing Team site") and \
        d1["report_utc"] == goalies._iso(at)
    d2 = g["persons"][person(p, "Delta G2")]
    assert d2["status"] == "NOT STARTING" and d2["repair"]
    assert "Delta G2" not in names_in(r.run.version_file(1), p, "7100000002")
    assert g["rotowire"]["line"].startswith("RotoWire news: stored")


def test_daily_faceoff_and_rotowire_confirming_different_goalies_is_conflicted_and_not_repaired(tmp_path, monkeypatch,
                                                                                               fake_codes, capsys):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [df_report("DDD", "Delta G2", created=BEFORE - timedelta(hours=2))])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()  # nothing repaired for that team
    g = r.manifest["goalies"]
    assert g["GOALIE_GATE"] == "CONFLICTED"
    assert g["persons"][person(p, "Delta G1")]["status"] == "CONFLICTED"
    assert any("RotoWire confirms Delta G1" in x and "names Delta G2" in x for x in g["problems"])
    from nhl_dfs.cli import _print_result

    _print_result(r)
    out = capsys.readouterr().out
    assert "CONFLICTED" in out and "RotoWire confirms Delta G1" in out


def test_daily_faceoff_expecting_another_goalie_is_conflicted(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [df_report("DDD", "Delta G2", created=BEFORE - timedelta(hours=2), state=GoalieState.EXPECTED)])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert r.manifest["goalies"]["persons"][person(p, "Delta G1")]["status"] == "CONFLICTED"


def test_daily_faceoff_expecting_the_same_goalie_is_confirmed_with_both_sources(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [df_report("DDD", "Delta G1", created=BEFORE - timedelta(hours=2), state=GoalieState.EXPECTED)])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    d1 = r.manifest["goalies"]["persons"][person(p, "Delta G1")]
    assert d1["status"] == "CONFIRMED" and d1["source"].startswith("RotoWire") and "Daily Faceoff expected" in d1["source"]
    assert "Delta G2" not in names_in(r.run.version_file(1), p, "7100000002")  # the Kochetkov case repairs


def test_the_later_rotowire_item_wins_and_the_earlier_is_printed(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=3)),
                                 rw_report("DDD", "Delta G2", at=BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    g = r.manifest["goalies"]
    assert g["persons"][person(p, "Delta G2")]["status"] == "CONFIRMED"
    assert any("earlier RotoWire item named Delta G1" in n for n in g["rotowire"]["notes"])


def test_rotowire_expected_is_never_a_repair_target(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=1), state=GoalieState.EXPECTED)]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    g = r.manifest["goalies"]["persons"]
    assert g[person(p, "Delta G1")]["status"] == "EXPECTED" and g[person(p, "Delta G2")]["status"] == "NOT EXPECTED"


def _skater_in(path, p, eid, pinned_ok=False):
    return next(p.by_role_id[x] for x in goalies.lineups_of(path, p)[eid] if x and not p.by_role_id[x].is_goalie)


def test_a_rotowire_scratch_keeps_the_player_out_of_free_cells(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    sk = _skater_in(cur, p, "7100000002")
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news(scratches=[rw.Scratch(sk.name, sk.team, f"{sk.name} will not play Thursday", rw.AGGREGATOR,
                                                      None, BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    assert r.statuses["FILE_VALID"] == "TRUE"
    after = goalies.lineups_of(r.run.version_file(1), p)
    assert all(sk.name not in {p.by_role_id[x].name for x in lu if x} for lu in after.values())
    assert f"{sk.name} ({sk.team})" in r.manifest["news"]["rotowire_scratches"]
    assert r.manifest["news"]["rotowire_scratches"][f"{sk.name} ({sk.team})"].startswith("RotoWire scratch")


def test_a_later_will_play_item_cancels_the_scratch(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    sk = _skater_in(cur, p, "7100000002")
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news(scratches=[
        rw.Scratch(sk.name, sk.team, "will not play Thursday", rw.AGGREGATOR, None, BEFORE - timedelta(hours=2)),
        rw.Scratch(sk.name, sk.team, "will play Thursday", rw.AGGREGATOR, None, BEFORE - timedelta(hours=1), cancel=True)]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert "rotowire_scratches" not in r.manifest["news"]


def test_a_scratch_in_a_pinned_cell_is_reported_and_the_cell_kept(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    eid = "7100000003"  # holds AAA@BBB players; that game has started at G1 + 10 min
    sk = next(p.by_role_id[x] for x in goalies.lineups_of(cur, p)[eid]
              if x and not p.by_role_id[x].is_goalie and p.by_role_id[x].team in ("AAA", "BBB"))
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news(scratches=[rw.Scratch(sk.name, sk.team, "will not play Thursday", rw.AGGREGATOR, None,
                                                      G1 - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur, as_of=G1 + timedelta(minutes=10))
    e_before = next(e for e in read_entries(cur).entries if e.entry_id == eid)
    e_after = next(e for e in read_entries(r.run.version_file(1)).entries if e.entry_id == eid)
    k = [i for i, x in enumerate(goalies.lineups_of(cur, p)[eid]) if x == sk.role_id][0]
    assert e_before.cells[k] == e_after.cells[k]
    assert any(m.startswith(f"entry {eid}") and sk.name in m and "pinned" in m for m in r.messages)


def test_a_scratch_of_the_confirmed_goalie_is_conflicted_and_not_excluded(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [df_report("DDD", "Delta G2", created=BEFORE - timedelta(hours=2))])
    inject_rw(monkeypatch, news(scratches=[rw.Scratch("Delta G2", "DDD", "Delta G2 will not play Thursday", rw.AGGREGATOR,
                                                      None, BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    g = r.manifest["goalies"]
    assert g["persons"][person(p, "Delta G2")]["status"] == "CONFLICTED" and "rotowire_scratches" not in r.manifest["news"]


def test_daily_faceoff_unread_is_still_no_news_with_rotowire_read(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    inject_df(monkeypatch, [], path="none")
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G2", at=BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    assert r.manifest["goalies"]["GOALIE_GATE"] == "NO_NEWS"


def test_an_unreadable_rotowire_page_leaves_the_board_as_daily_faceoff_alone_and_says_so(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    dfr = [df_report("DDD", "Delta G1", created=BEFORE - timedelta(hours=1))]
    inject_df(monkeypatch, dfr)
    inject_rw(monkeypatch, None)  # the Daily Faceoff-only board, as before C54
    only = swap(tmp_path, b, cur)
    b2 = base(tmp_path / "two")
    inject_rw(monkeypatch, news(source="none", notes=["RotoWire unavailable (SourceSchemaError: rotowire: no news items)"]))
    r = late_swap.run(b2.run.run_id, cur, runs_root=tmp_path / "two" / "runs", as_of=BEFORE, offline=True)
    assert r.run.version_file(1).read_bytes() == only.run.version_file(1).read_bytes()
    g = r.manifest["goalies"]
    assert {k: v["status"] for k, v in g["persons"].items()} == \
        {k: v["status"] for k, v in only.manifest["goalies"]["persons"].items()}
    assert g["rotowire"]["line"].startswith("RotoWire unavailable (") and g["rotowire"]["line"].endswith(
        "board from Daily Faceoff alone")
    assert any(m.startswith("RotoWire unavailable") for m in r.messages)


# -- the research request (card) ------------------------------------------------------------------------------------

def test_the_research_requests_urls_include_the_feed(tmp_path):
    r = run_slate(LS / "classic" / "DKSalaries.csv", LS / "classic" / "DKEntries.template.csv", offline=True,
                  baseline_only=True, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs", clock=lambda: BEFORE)
    req = packet.research_request(r.run, now=BEFORE)
    assert "https://www.rotowire.com/hockey/news.php" in req["urls"]
    assert req["urls"][0] == "https://www.dailyfaceoff.com/starting-goalies"
    json.dumps(req)


# -- C54 review: negations, hedges, subjects, attempts, timeouts ------------------------------------------------------

@pytest.mark.parametrize("text", [  # CONSTRUCTED: each was read as a start, scratch or cancel before the review fix
    "Kochetkov hasn't been confirmed as the starter for Saturday.",
    "Kochetkov is not confirmed as the starter Saturday.",
    "It's unclear whether Kochetkov will start Saturday.",
    "Kochetkov will start Saturday if Andersen is unable to go.",
    "Kochetkov will start skating Saturday.",
    "Kochetkov will start practicing tonight.",
    "Kochetkov is expected to be confirmed as the starter Saturday.",
    "Kochetkov has not been ruled out for Saturday.",
    "Kochetkov hasn't been ruled out for Saturday.",
    "Kochetkov could be scratched Saturday.",
    "Kochetkov is not expected to be scratched Saturday.",
    "Kochetkov will not be scratched Saturday.",
    "Kochetkov will miss Saturday's practice.",
    "Kochetkov hasn't been cleared to play Saturday.",
    "Kochetkov might not start Saturday.",
    "Kochetkov may not start Saturday.",
])
def test_a_negated_hedged_or_conditional_statement_never_counts(text):
    assert kind(text) == "ignored_statement"


def test_the_subject_is_the_items_player_never_another_name_or_a_namesake():  # CONSTRUCTED
    t = "Andersen will sit, so the Hurricanes will start Kochetkov tonight."
    assert kind(t, player="Frederik Andersen") == "ignored_subject" and kind(t) == "start"
    t = "Andersen will back up tonight. Kochetkov was sharp Thursday, and he will start against the Lightning."
    assert kind(t, player="Frederik Andersen") == "ignored_subject" and kind(t) == "start"
    t = "Jack Hughes will not play tonight against the Lightning."
    assert kind(t, player="Luke Hughes") == "ignored_subject" and kind(t, player="Jack Hughes") == "scratch"
    assert kind("Kochetkov will start vs. Tampa Bay.") == "start"  # "vs." does not end the sentence


def test_a_start_for_a_skater_is_counted_as_ignored_not_as_a_start():
    it = rw.NewsItem("Sebastian Aho", "CAR", "C", "", "Aho will start tonight against the Lightning.",
                     datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), None)
    out = rw.read_items([it], {"CAR": SAT}, datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch())
    assert out.reports == [] and out.counts.get("ignored_not_goalie") == 1 and "start" not in out.counts


def test_a_naive_time_is_skipped_never_raised():
    it = rw.NewsItem("Pyotr Kochetkov", "CAR", "G", "", "Kochetkov will start tonight.", datetime(2026, 10, 10, 15, 0), None)
    out = rw.read_items([it], {"CAR": SAT}, datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc), rw.NewsFetch())
    assert out.counts.get("skipped_no_time") == 1


def test_an_offline_read_never_writes_an_attempt(tmp_path):
    c, t = _cache(tmp_path, [("rotowire.com", 200, b"")], offline=True)
    rw.fetch_news({}, cache=c, now=BEFORE)
    assert not (tmp_path / "raw" / "rotowire" / "attempts.json").exists() and t.calls == []


def test_the_attempt_is_written_before_the_request_so_a_crashed_read_sends_no_second_one(tmp_path):
    clock = {"t": BEFORE}
    c, t = _cache(tmp_path, [("rotowire.com", 200, RuntimeError("connection dropped mid-read"))], clock=lambda: clock["t"])
    got = rw.fetch_news({}, cache=c, now=BEFORE)
    assert got.source == "none" and len(t.calls) == 1
    clock["t"] = BEFORE + timedelta(seconds=300)
    rw.fetch_news({}, cache=c, now=clock["t"])
    assert len(t.calls) == 1


def test_a_server_error_is_one_request_not_a_retry(tmp_path):
    c, t = _cache(tmp_path, [("rotowire.com", 503, b"Service Unavailable")])
    rw.fetch_news({}, cache=c, now=BEFORE)
    assert len(t.calls) == 1


@pytest.fixture
def fake_parser(monkeypatch):
    """A stand-in parser for the cache tests only (the bodies below are not pages): a body parses when it starts
    with 'TESTBODY'."""
    def parse(html):
        if not isinstance(html, str) or not html.startswith("TESTBODY"):
            raise rw.SourceSchemaError("rotowire: not a test body")
        return []
    monkeypatch.setattr(rw, "parse_news_page", parse)


def test_a_second_call_inside_the_ttl_reads_the_cache(tmp_path, fake_parser):
    clock = {"t": BEFORE}
    c, t = _cache(tmp_path, [("rotowire.com", 200, b"TESTBODY 1")], clock=lambda: clock["t"])
    assert rw.fetch_news({}, cache=c, now=BEFORE).source == "live"
    clock["t"] = BEFORE + timedelta(seconds=599)
    again = rw.fetch_news({}, cache=c, now=clock["t"])
    assert again.source == "stored" and len(t.calls) == 1


def test_a_failed_request_falls_back_to_the_stored_copy(tmp_path, fake_parser):
    clock = {"t": BEFORE}
    routes = [("rotowire.com", 200, b"TESTBODY 1")]
    c, t = _cache(tmp_path, routes, clock=lambda: clock["t"])
    rw.fetch_news({}, cache=c, now=BEFORE)
    routes[0] = ("rotowire.com", 403, b"Access Denied")
    clock["t"] = BEFORE + timedelta(minutes=15)
    got = rw.fetch_news({}, cache=c, now=clock["t"])
    assert len(t.calls) == 2 and got.source == "stored" and got.fetched_utc == BEFORE
    assert any("request failed" in n for n in got.notes)


def test_a_rotowire_timeout_uses_the_stored_copy_and_says_so(monkeypatch, fake_codes):
    import time as time_mod

    calls = []

    def slow(games, *, cache, now):
        calls.append(cache.offline)
        if not cache.offline:
            time_mod.sleep(1.0)
        return rw.NewsFetch(source="stored", fetched_utc=now)
    monkeypatch.setattr(rw, "fetch_news", slow)
    got = goalies.gather_rotowire(pool(), HttpCache(offline=False, transport=lambda *a: (500, b"", {})),
                                  offline=False, budget_s=0.2, now=BEFORE)
    assert calls[-1] is True and got.notes[0].startswith("RotoWire page not fetched (TimeoutError")


def test_a_rotowire_read_that_never_returns_leaves_the_board_as_daily_faceoff_alone(monkeypatch, fake_codes):
    import time as time_mod

    monkeypatch.setattr(goalies, "gather_rotowire", lambda *a, **k: time_mod.sleep(3.0))
    p = pool()
    inject_df(monkeypatch, [df_report("DDD", "Delta G1", created=BEFORE - timedelta(hours=1))])
    b = goalies.build(p, now=BEFORE, offline=True, budget_s=0.1)
    assert b.rotowire["line"].startswith("RotoWire unavailable (RotoWire read did not finish")
    assert b.persons[person(p, "Delta G2")].repair  # Daily Faceoff's confirmation still acts


def test_a_rotowire_scratched_goalie_with_no_other_source_is_repaired(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news(scratches=[rw.Scratch("Delta G2", "DDD", "Delta G2 will not play Thursday", rw.AGGREGATOR,
                                                      None, BEFORE - timedelta(hours=1))]))
    r = swap(tmp_path, b, cur)
    g = r.manifest["goalies"]["persons"][person(p, "Delta G2")]
    assert g["status"] == "NOT STARTING" and g["source"].startswith("RotoWire scratch")
    assert "Delta G2" not in names_in(r.run.version_file(1), p, "7100000002")
    assert "Delta G2 (DDD)" in r.manifest["news"]["rotowire_scratches"]


def test_two_agreeing_confirmations_beat_the_draftkings_flag(tmp_path, monkeypatch, fake_codes):
    import csv
    import io

    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    rows = list(csv.reader(io.StringIO((LS / "classic" / "DKSalaries.csv").read_bytes().decode("utf-8-sig"), newline="")))
    for rec in rows[1:]:
        if rec and rec[2] == "Delta G2":
            rec[10] = "P"
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    fresh = tmp_path / "DKSalaries (1).csv"
    fresh.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    inject_df(monkeypatch, [df_report("DDD", "Delta G1", created=BEFORE - timedelta(hours=2))])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=1))]))
    r = late_swap.run(b.run.run_id, cur, runs_root=tmp_path / "runs", as_of=BEFORE, offline=True, salary_path=fresh)
    d1 = r.manifest["goalies"]["persons"][person(p, "Delta G1")]
    assert d1["status"] == "CONFIRMED" and "Daily Faceoff" in d1["source"] and d1["source"].startswith("RotoWire")
    assert "Delta G2" not in names_in(r.run.version_file(1), p, "7100000002")


def test_a_later_rotowire_expectation_for_another_goalie_is_conflicted(tmp_path, monkeypatch, fake_codes):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [])
    inject_rw(monkeypatch, news([rw_report("DDD", "Delta G1", at=BEFORE - timedelta(hours=3)),
                                 rw_report("DDD", "Delta G2", at=BEFORE - timedelta(hours=1), state=GoalieState.EXPECTED)]))
    r = swap(tmp_path, b, cur)
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert r.manifest["goalies"]["persons"][person(p, "Delta G1")]["status"] == "CONFLICTED"


# -- the recorded page (C54 part 2; flags 93, 94) --------------------------------------------------------------------
# tests/fixtures/http/rotowire_hockey_news_recaps.html: the real page of 2026-10-12T12:58:42Z, trimmed by deleting whole
# byte ranges (make_rotowire_fixture.py; SOURCES.md). Its six items are Sunday 2026-10-11 recaps of CAR @ PHI, VAN @ NYR
# and SEA @ WSH. Tests marked CONSTRUCTED change a copy in memory to reach an error path; nothing is stored.

import re  # noqa: E402

import yaml  # noqa: E402

HTTP = TESTS / "fixtures" / "http"
RECAPS = HTTP / "rotowire_hockey_news_recaps.html"
RECAPS_READ = datetime(2026, 10, 12, 12, 58, 42, tzinfo=timezone.utc)  # the recording's request time (SOURCES.md)
SUNDAY = date(2026, 10, 11)


def _page(path):
    return path.read_bytes().decode("utf-8")  # as HttpCache decodes an html body


def _recap_games(day):
    """The recaps' real games (NHL schedule of 2026-10-11), placed on `day`."""
    out = {}
    for away, home, hour in (("SEA", "WSH", 21), ("VAN", "NYR", 22), ("CAR", "PHI", 23)):
        t = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        out[home], out[away] = rw.SlateGame(home, away, day, t), rw.SlateGame(away, home, day, t)
    return out


def _attempts(root):
    p = root / "rotowire" / "attempts.json"
    return p.read_text(encoding="utf-8") if p.exists() else None


def test_the_recorded_page_parses_into_its_items_newest_first():
    items = rw.parse_news_page(_page(RECAPS))
    assert [(i.player, i.team, i.position, i.news_id) for i in items] == [
        ("Joseph Woll", "PHI", "G", 595578), ("Pyotr Kochetkov", "CAR", "G", 595572),
        ("Elias Pettersson", "VAN", "D", 595571), ("Dylan Garand", "NYR", "G", 595561),
        ("Alex Ovechkin", "WSH", "LW", 595558), ("Ben Meyers", "SEA", "LW", 595556)]
    assert [i.page_rank for i in items] == list(range(6))
    k = items[1]
    assert k.headline == "Sharp in win"
    assert k.news == "Kochetkov stopped 18 of 19 shots in Sunday's 4-1 win over the Flyers."
    assert k.url == "https://www.rotowire.com/hockey/headlines/pyotr-kochetkov-news-sharp-in-win-595572"
    assert k.published_utc is None and k.published_day == SUNDAY  # the page prints a date only (flag 94)
    assert k.cited_url is None and k.cited_name is None  # no item on the page links a post: RotoWire (aggregator)
    assert all(i.published_day == SUNDAY and i.published_utc is None for i in items)


def test_the_analysis_paragraph_never_reaches_the_news():
    raw = _page(RECAPS)
    assert raw.count('class="news-update__analysis"') == 6 and "reveal our take" in raw
    for i in rw.parse_news_page(raw):
        assert "ANALYSIS" not in i.news + i.headline and "reveal our take" not in i.news + i.headline


def test_every_team_code_in_the_pages_own_filter_is_an_nhl_code_mapped_to_itself():
    codes = re.findall(r'news\.php\?team=([A-Z]{2,3})"', _page(RECAPS))
    with open(rw.TEAMS_YAML, encoding="utf-8") as f:
        nhl = {t["nhl"] for t in yaml.safe_load(f)["teams"]}
    assert len(codes) == 32 and set(codes) == nhl
    tm = rw.team_map()
    assert all(tm[c] == c for c in codes)


def test_a_team_code_not_in_the_table_is_reported_and_not_read():  # CONSTRUCTED
    it = rw.NewsItem("Pyotr Kochetkov", "CRL", "G", "", "Kochetkov will start tonight against the Lightning.", None,
                     None, published_day=SAT.day, news_id=1)
    read = SAT.start_utc - timedelta(hours=1)
    out = rw.read_items([it], {"CAR": SAT, "TBL": SAT}, read, rw.NewsFetch(), fetched_utc=read)
    assert out.counts.get("unknown_team_code") == 1 and not out.reports
    assert any("'CRL'" in n for n in out.notes)


def test_dated_items_count_on_their_own_day_only():
    items = rw.parse_news_page(_page(RECAPS))
    same = rw.read_items(items, _recap_games(SUNDAY), RECAPS_READ, rw.NewsFetch(), fetched_utc=RECAPS_READ)
    assert same.counts == {"items": 6, "dated_only": 6, "today": 6, "ignored_statement": 6}  # recaps state nothing
    assert not same.reports and not same.scratches
    nxt = rw.read_items(items, _recap_games(SUNDAY + timedelta(days=1)), RECAPS_READ, rw.NewsFetch(),
                        fetched_utc=RECAPS_READ)
    assert nxt.counts == {"items": 6, "skipped_other_day": 6}


@pytest.mark.parametrize("now, counted", [
    (RECAPS_READ, True),
    (RECAPS_READ - timedelta(seconds=59), True),  # a page read just after the clock, as a live read is (flag 94 b)
    (RECAPS_READ - timedelta(seconds=61), False),  # a rehearsal or replay clock before the read: nothing counts
    (RECAPS_READ - timedelta(hours=2), False),  # live or stored alike (flag 94 b as amended)
])
def test_a_dated_item_counts_only_when_the_page_was_read_by_the_clock(now, counted):
    items = rw.parse_news_page(_page(RECAPS))
    out = rw.read_items(items, _recap_games(SUNDAY), now, rw.NewsFetch(), fetched_utc=RECAPS_READ)
    assert out.counts.get("today", 0) == (6 if counted else 0)
    assert out.counts.get("skipped_after_clock", 0) == (0 if counted else 6)


def test_a_dated_item_without_a_read_time_is_skipped():
    items = rw.parse_news_page(_page(RECAPS))
    out = rw.read_items(items, _recap_games(SUNDAY), RECAPS_READ, rw.NewsFetch())
    assert out.counts == {"items": 6, "skipped_no_time": 6}


@pytest.mark.parametrize("text, utc, day", [  # CONSTRUCTED strings: the page prints the first form only
    ("October 11, 2026", None, date(2026, 10, 11)),
    ("Oct. 31, 2026 7:00 PM", datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc), date(2026, 10, 31)),  # EDT
    ("November 2, 2026 7:00 PM ET", datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc), date(2026, 11, 2)),  # EST
    ("November 2, 2026 6:00 PM CT", datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc), date(2026, 11, 2)),
    ("Sept 30, 2026 12:05 AM", datetime(2026, 9, 30, 4, 5, tzinfo=timezone.utc), date(2026, 9, 30)),
])
def test_the_timestamp_reader(text, utc, day):
    assert rw.parse_stamp(text) == (utc, day)


@pytest.mark.parametrize("text", ["3 hours ago", "Octember 1, 2026", "October 11, 2026 7:00 PM XT", ""])
def test_an_unreadable_timestamp_is_an_error_never_a_guess(text):
    with pytest.raises(ValueError):
        rw.parse_stamp(text)


def test_an_item_without_a_player_or_a_date_is_skipped_and_counted():  # CONSTRUCTED: whole elements removed in memory
    raw = _page(RECAPS)
    raw = re.sub(r'<a class="news-update__player-link" href="/hockey/player/pyotr-kochetkov[^"]*">[^<]*</a>', "", raw)
    raw = re.sub(r'(<div class="news-update__timestamp">)October 11, 2026(</div>\s*<div class="news-update__news">Woll)',
                 r"\1\2", raw)
    items = rw.parse_news_page(raw)
    assert len(items) == 6 and items[1].player == "" and items[0].published_day is None
    out = rw.read_items(items, _recap_games(SUNDAY), RECAPS_READ, rw.NewsFetch(), fetched_utc=RECAPS_READ)
    assert out.counts["skipped_malformed"] == 1 and out.counts["skipped_no_time"] == 1 and out.counts["today"] == 4


def _no_items(raw):
    return raw[:raw.index('<div class="news-update">')] + raw[raw.index("</main>"):]


def test_a_page_without_news_items_raises_and_the_board_says_daily_faceoff_alone(tmp_path):  # CONSTRUCTED
    raw = _page(RECAPS)
    with pytest.raises(rw.SourceSchemaError, match="no news items"):
        rw.parse_news_page(_no_items(raw))
    undated = re.sub(r'(<div class="news-update__timestamp">)[^<]*(</div>)', r"\1\2", raw)
    with pytest.raises(rw.SourceSchemaError, match="no news item has a player and a readable date"):
        rw.parse_news_page(undated)
    c, t = _cache(tmp_path, [("rotowire.com", 200, _no_items(raw).encode("utf-8"))], clock=lambda: RECAPS_READ)
    got = rw.fetch_news(_recap_games(SUNDAY), cache=c, now=RECAPS_READ)
    assert got.source == "none" and len(t.calls) == 1 and not got.reports
    line = goalies._rw_line(got, {}, RECAPS_READ)
    assert line.startswith("RotoWire unavailable (") and line.endswith("board from Daily Faceoff alone")


def test_a_second_read_inside_the_ttl_uses_the_stored_real_page_and_offline_sends_nothing(tmp_path):
    clock = {"t": RECAPS_READ}
    c, t = _cache(tmp_path, [("rotowire.com", 200, RECAPS.read_bytes())], clock=lambda: clock["t"])
    games = _recap_games(SUNDAY)
    first = rw.fetch_news(games, cache=c, now=RECAPS_READ - timedelta(seconds=2))  # the read lands after the clock
    assert first.source == "live" and len(t.calls) == 1 and first.counts["today"] == 6
    clock["t"] = RECAPS_READ + timedelta(seconds=599)
    again = rw.fetch_news(games, cache=c, now=clock["t"])
    assert again.source == "stored" and len(t.calls) == 1 and again.counts == first.counts
    assert again.fetched_utc == RECAPS_READ
    before = _attempts(tmp_path / "raw")
    off, t2 = _cache(tmp_path, [("rotowire.com", 200, b"")], offline=True,
                     clock=lambda: RECAPS_READ + timedelta(hours=3))
    late = rw.fetch_news(games, cache=off, now=RECAPS_READ + timedelta(hours=3))
    assert late.source == "stored" and t2.calls == [] and _attempts(tmp_path / "raw") == before
    assert late.counts["today"] == 6


def test_page_order_decides_later_between_dated_items():  # CONSTRUCTED items, the page's newest-first order
    day, read = SAT.day, SAT.start_utc - timedelta(hours=2)
    def item(nid, text, player="Pyotr Kochetkov"):
        return rw.NewsItem(player, "CAR", "G", "", text, None, None, published_day=day, news_id=nid)
    page = [item(12, "Andersen will start tonight against the Lightning.", "Frederik Andersen"),
            item(11, "Kochetkov will start tonight against the Lightning."),
            item(10, "Jarvis was scratched for tonight's game against the Lightning.", "Seth Jarvis"),
            item(13, "Jarvis will play tonight against the Lightning.", "Seth Jarvis")]
    page = [page[3]] + page[:3]  # newest first, as the page lists them
    out = rw.read_items(page, {"CAR": SAT, "TBL": SAT}, SAT.start_utc, rw.NewsFetch(), fetched_utc=read)
    assert [r.goalie_name for r in out.reports] == ["Pyotr Kochetkov", "Frederik Andersen"]  # oldest first
    assert all(r.news_created_utc == read for r in out.reports)  # the page's read time (flag 94 c)
    assert [(s.player, s.cancel) for s in out.scratches] == [("Seth Jarvis", False), ("Seth Jarvis", True)]
    fetch = SimpleNamespace(reports=out.reports)
    assert goalies._rw_later(fetch, out.reports[1], out.reports[0])
    assert not goalies._rw_later(fetch, out.reports[0], out.reports[1])


def test_a_dated_item_after_the_reads_own_day_is_skipped():  # CONSTRUCTED: the zone reading of flag 94 (a) failing
    items = rw.parse_news_page(_page(RECAPS))
    early = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)  # 23:00 New York on 10-10: the page cannot date 10-11
    out = rw.read_items(items, _recap_games(SUNDAY), early, rw.NewsFetch(), fetched_utc=early)
    assert out.counts == {"items": 6, "skipped_future_date": 6}


def test_a_missing_end_tag_in_one_item_leaves_the_others_whole():  # CONSTRUCTED: one end tag removed in memory
    raw = _page(RECAPS)
    i = raw.index('<div class="news-update__news">Kochetkov')
    j = raw.index("</div>", i)
    broken = raw[:j] + raw[j + len("</div>"):]
    assert [(x.player, x.team, x.news_id, x.published_day) for x in rw.parse_news_page(broken)] == \
        [(x.player, x.team, x.news_id, x.published_day) for x in rw.parse_news_page(raw)]
    stray = raw.replace('<div class="news-update__main">', '</span><div class="news-update__main">', 1)
    assert len(rw.parse_news_page(stray)) == 6


@pytest.mark.parametrize("expected_last, status", [(True, "CONFLICTED"), (False, "CONFIRMED")])
def test_page_order_breaks_a_time_tie_on_the_board(tmp_path, monkeypatch, fake_codes, expected_last, status):
    b = base(tmp_path)
    cur = clean_current(tmp_path)
    p = pool()
    inject_df(monkeypatch, [])
    at = BEFORE - timedelta(minutes=30)
    start = rw_report("DDD", "Delta G1", at=at)
    exp = rw_report("DDD", "Delta G2", at=at, state=GoalieState.EXPECTED)
    inject_rw(monkeypatch, news([start, exp] if expected_last else [exp, start], fetched=at))
    g = swap(tmp_path, b, cur).manifest["goalies"]
    assert g["persons"][person(p, "Delta G1")]["status"] == status
    if expected_last:
        assert any("a later RotoWire item (expected, " in x and "page read time" in x for x in g["problems"])


# -- the second recording: today's goalie news (flag 86 as amended after it) -------------------------------------------
# tests/fixtures/http/rotowire_hockey_news.html: the real page of 2026-10-12T15:05:09Z, trimmed to five whole items
# (SOURCES.md). Today's real games (NHL schedule): FLA @ BUF 17:00Z, OTT @ NJD 23:00Z, VGK @ MIN 00:00Z on 10-13.

NEWS = HTTP / "rotowire_hockey_news.html"
NEWS_READ = datetime(2026, 10, 12, 15, 5, 9, tzinfo=timezone.utc)
MONDAY = date(2026, 10, 12)
DARRAGH = "https://x.com/darraghfla/status/2109654471366045721"


def _monday_games(day=MONDAY):
    out = {}
    for away, home, start in (("FLA", "BUF", datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)),
                              ("OTT", "NJD", datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)),
                              ("VGK", "MIN", datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc))):
        start = start + (datetime.combine(day, start.timetz()) - datetime.combine(MONDAY, start.timetz()))
        out[home], out[away] = rw.SlateGame(home, away, day, start), rw.SlateGame(away, home, day, start)
    return out


def test_the_second_page_parses_with_its_cited_posts_and_both_days():
    items = rw.parse_news_page(_page(NEWS))
    assert [(i.news_id, i.player, i.team, i.position, i.published_day) for i in items] == [
        (595587, "Nico Daws", "NJD", "G", MONDAY), (595586, "Jacob Markstrom", "FLA", "G", MONDAY),
        (595582, "Connor Brown", "NJD", "RW", MONDAY), (595581, "Stefan Noesen", "NJD", "C", MONDAY),
        (595572, "Pyotr Kochetkov", "CAR", "G", SUNDAY)]
    m = items[1]
    assert m.headline == "Starting in Buffalo"
    assert m.news == ("Markstrom will defend the road net against the Sabres on Monday, Rob Darragh of the Panthers' "
                      "official site reports.")
    assert (m.cited_name, m.cited_url) == ("Rob Darragh of the Panthers' official site", DARRAGH)
    assert m.url == "https://www.rotowire.com/hockey/headlines/jacob-markstrom-news-starting-in-buffalo-595586"
    assert (items[2].cited_name, items[3].cited_name) == ("@NJDevils on X", "@NJDevils on X")  # a link on 'placed'
    assert "Lower Body" not in items[2].news + items[2].headline  # the injury item's body-part line is not news


@pytest.mark.parametrize("nid, kind_", [
    (595586, "start"),  # will defend the road net against the Sabres on Monday (flag 86 as amended)
    (595587, "ignored_statement"),  # first goalie off the ice, indicating he'll be between the home pipes: a hedge
    (595582, "ignored_statement"),  # placed on injured reserve: never a scratch (flag 86)
    (595581, "ignored_statement"),  # recalled from AHL Utica
])
def test_the_pages_real_sentences_classify_as_the_flags_say(nid, kind_):
    it = next(i for i in rw.parse_news_page(_page(NEWS)) if i.news_id == nid)
    assert rw.read_item(it, _monday_games()[it.team], rw.team_names())[0] == kind_


def test_the_pages_confirmation_reaches_a_goalie_row_with_its_cited_post_and_the_read_time(tmp_path):
    c, t = _cache(tmp_path, [("rotowire.com", 200, NEWS.read_bytes())], clock=lambda: NEWS_READ)
    got = rw.fetch_news(_monday_games(), cache=c, now=NEWS_READ - timedelta(seconds=1))
    assert got.source == "live" and len(t.calls) == 1
    assert got.counts == {"items": 5, "skipped_other_teams": 1, "dated_only": 4, "today": 4, "ignored_statement": 3,
                          "start": 1}
    (r,) = got.reports
    assert (r.team, r.goalie_name, r.state, r.opponent, r.game_date) == ("FLA", "Jacob Markstrom",
                                                                         GoalieState.CONFIRMED, "BUF", MONDAY)
    assert (r.source_name, r.source_url, r.strength_raw) == ("Rob Darragh of the Panthers' official site", DARRAGH,
                                                              "will defend the road net")
    assert r.news_created_utc == NEWS_READ == got.fetched_utc  # the page's read time (flag 94 c)
    assert not got.scratches
    line = goalies._rw_line(got, {}, NEWS_READ)
    assert "1 start(s)" in line and "RotoWire times are when the page was read" in line


def test_the_pages_confirmation_does_not_count_for_the_next_day_or_from_late_last_night():
    items = rw.parse_news_page(_page(NEWS))
    nxt = rw.read_items(items, _monday_games(MONDAY + timedelta(days=1)), NEWS_READ + timedelta(days=1),
                        rw.NewsFetch(), fetched_utc=NEWS_READ)
    assert nxt.counts.get("start", 0) == 0 and nxt.counts["skipped_other_day"] == 4
    late = datetime(2026, 10, 11, 23, 30, tzinfo=rw.ET)  # CONSTRUCTED: a timed item at 23:30 New York the day before
    it = rw.NewsItem("Jacob Markstrom", "FLA", "G", "", "Markstrom will defend the road net against the Sabres on "
                     "Monday.", late.astimezone(timezone.utc), None)
    out = rw.read_items([it], _monday_games(), NEWS_READ, rw.NewsFetch(), fetched_utc=NEWS_READ)
    assert out.counts == {"items": 1, "skipped_other_day": 1}


@pytest.mark.parametrize("text, expected", [  # CONSTRUCTED: the amended phrase family on the Saturday game
    ("Kochetkov will defend the road net against the Lightning on Saturday.", "start"),
    ("Kochetkov will guard the home goal Saturday against the Lightning.", "start"),
    ("Kochetkov was first off the ice Saturday, indicating he will start against the Lightning.", "ignored_statement"),
    ("Kochetkov will start against the Lightning on Saturday, suggesting Andersen is still hurt.", "start"),
    ("Kochetkov will start against the Lightning on Saturday, suggesting a rest day Sunday.", "ignored_other_game"),
])
def test_home_or_road_before_the_net_word_and_the_new_hedges(text, expected):
    assert kind(text) == expected


# -- the card's exit check: a refresh on the recorded page prints RotoWire on the GOALIES line ------------------------

C54_SLATE = TESTS / "fixtures" / "c54_slate"  # make_fixtures.py: FLA @ BUF and VGK @ MIN of 2026-10-12


def test_exit_check_a_refresh_on_the_recorded_page_prints_the_rotowire_source_on_the_goalies_line(
        tmp_path, monkeypatch, capsys):
    """No setting redirects data/raw, so the default cache root points at a scratch folder that the engine itself
    seeds with the recorded page: one online read through a fake transport at the recorded time. The run and the
    delivery happen at 14:00Z, before the page was read (so it cannot count); the refresh runs offline at 15:30Z."""
    import nhl_dfs.data.http as http_mod
    from nhl_dfs import cli

    raw = tmp_path / "raw"
    monkeypatch.setattr(http_mod, "DEFAULT_ROOT", raw)
    c, t = _cache(tmp_path, [("rotowire.com", 200, NEWS.read_bytes())], clock=lambda: NEWS_READ)
    assert rw.fetch_news({}, cache=c, now=NEWS_READ).source == "live" and len(t.calls) == 1
    seeded = _attempts(raw)
    t0 = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)
    runs, outs = tmp_path / "runs", tmp_path / "outputs"
    b = run_slate(C54_SLATE / "DKSalaries.csv", C54_SLATE / "DKEntries.template.csv", offline=True, out_root=runs,
                  clock=lambda: t0)
    assert b.statuses["FILE_VALID"] == "TRUE"
    d = late_swap.run(b.run.run_id, C54_SLATE / "DKEntries.current.csv", offline=True, fast=True, runs_root=runs,
                      outputs_root=outs, clock=lambda: t0)
    assert d.statuses["DELIVERY_STATUS"] == "CHECKED"
    before = d.manifest["goalies"]
    # NJD and CAR are not on this slate; Markstrom's item was read at 15:05Z, after the 14:00Z clock
    assert before["rotowire"]["counts"] == {"items": 5, "skipped_other_teams": 4, "skipped_after_clock": 1}
    capsys.readouterr()
    assert cli.main(["refresh", "--run", b.run.run_id, "--offline", "--as-of", "2026-10-12T15:30:00Z",
                     "--runs-root", str(runs), "--outputs-root", str(outs)]) == 0
    out = capsys.readouterr().out
    row = next(x for x in out.splitlines() if x.startswith("  Jacob Markstrom | FLA |"))
    cells = [x.strip() for x in row.split("|")]
    assert cells[2] == "CONFIRMED" and cells[4] == "Jacob Markstrom (CONFIRMED)", row
    assert cells[5].startswith("RotoWire (citing Rob Darragh of the Panthers' official site, dated item"), row
    assert "RotoWire news: stored" in out and "1 start(s)" in out
    assert not any(x.startswith("  FLA G2 |") for x in out.splitlines())  # the teammate's open cell was repaired
    assert _attempts(raw) == seeded and len(t.calls) == 1  # the offline refresh sent nothing
