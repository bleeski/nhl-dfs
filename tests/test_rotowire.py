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
