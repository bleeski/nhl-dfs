"""C39 commit 2 (B70): the incremental history refresh, the back-to-back goalie flag it feeds, and the safety of both.

Fixture store: the recorded 2025-10-09 game of tests/test_nhl_reports.py (DAL Oettinger and WPG Hellebuyck both started).
"old_store" is the same rows moved to 2025-10-07 under another game id, so a refresh has something older to build on and one
new game (10-09) to find. Nothing here touches the network (conftest refuses a real transport) or the real store (conftest
points it at an empty folder).
"""

import hashlib
import json
import shutil
import threading
import time
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from conftest import FakeTransport, mini_pair
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.run import run_slate
from nhl_dfs.data.features import asof
from nhl_dfs.data.features.asof import FeatureFrame
from nhl_dfs.data.history import nhl_reports, refresh, status
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.http import HttpCache, load_sources_config
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models import goalies as goalie_mod
from nhl_dfs.models import params
from nhl_dfs.models.rates import load_model_config
from test_nhl_reports import H, ROUTES, SEASON, run

pytestmark = pytest.mark.c39

AS_OF = date(2025, 10, 10)
NOW = datetime(2025, 10, 10, 16, 0, tzinfo=timezone.utc)  # 12:00 ET: the day through 10-09 is complete
OETTINGER = 8479979
KINDS = ("nhl_skater_games", "nhl_goalie_games", "skater_games", "goalie_games")


# -- the game day is the Eastern date (a defect in the way of the flag firing) -----------------------------------------------

def _goalie_started(team: str, day: date, nid: int) -> pd.DataFrame:
    return pd.DataFrame([{"nhl_id": nid, "game_id": 2026020001, "game_date": day, "team": team, "opponent": "SEA",
                          "regime": "regular", "started": True, "toi_s": 3600.0, "shots_against": 30.0, "goals_against": 2.0,
                          "xga": 2.85, "xga_missing": 1}])


@pytest.mark.parametrize("team, label", [("VAN", "10:00 PM ET start (02:00 UTC the next day)"), ("NYI", "7:00 PM ET start")])
def test_back_to_back_flag_fires_whatever_the_start_time(team, label):
    """VAN@EDM starts 09/29 10:00 PM ET, which is 2026-09-30 in UTC. The goalie who started on the 28th played the night
    before in Eastern dates, so his flag must be 1; comparing against the UTC date (the old rule) read 0 for any game
    starting after 8 PM ET."""
    pool = read_salary(mini_pair("classic")[0])
    people = params._person_rows(pool)
    pk = next(k for k, r in people.items() if r.team == team and r.position == "G")
    ff = FeatureFrame(date(2026, 9, 29), [20262027], pd.DataFrame(), _goalie_started(team, date(2026, 9, 28), 8470001),
                      pd.DataFrame(), pd.DataFrame())
    table = params.build(pool, {pk: 8470001}, date(2026, 9, 29), features=ff, line_games=pd.DataFrame())
    assert table.persons[pk].goalie.b2b == 1, label
    rested = FeatureFrame(date(2026, 9, 29), [20262027], pd.DataFrame(), _goalie_started(team, date(2026, 9, 27), 8470001),
                          pd.DataFrame(), pd.DataFrame())
    assert params.build(pool, {pk: 8470001}, date(2026, 9, 29), features=rested,
                        line_games=pd.DataFrame()).persons[pk].goalie.b2b == 0


# -- fixtures --------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    """(one_game, old): the 10-09 game only, and the same rows moved to 10-07 under another game id (10-09 not yet fetched)."""
    tmp = tmp_path_factory.mktemp("c39refresh")
    run(tmp, moneypuck=False, sub="one")
    one = tmp / "one" / "store"
    old = tmp / "old"
    for kind in KINDS:
        df = store_mod.read(kind, [SEASON], root=one)
        store_mod.write(kind, SEASON, df.assign(game_id=2025020001, game_date=date(2025, 10, 7)), root=old)
    return one, old


def _copy(src, tmp_path, name="store"):
    dst = tmp_path / name
    shutil.copytree(src, dst)
    return dst


def _cache(tmp_path, routes=ROUTES, **kw):
    cfg = load_sources_config()
    transport = FakeTransport(routes)
    return HttpCache(tmp_path / "raw", config=cfg, transport=transport, sleep=lambda s: None, **kw), transport


def _digest(root) -> dict:
    return {k: hashlib.sha256(store_mod.path_for(k, SEASON, root=root).read_bytes()).hexdigest() for k in KINDS
            if store_mod.path_for(k, SEASON, root=root).exists()}


def _reports(transport) -> list[str]:
    """The NHL per-game report requests the transport saw (a run also fetches Daily Faceoff and other pages)."""
    return [u for u in transport.calls if "stats/rest" in u]


def _b2b(root, nid=OETTINGER):
    cfg = load_model_config()
    f = asof.frame(AS_OF, None, store_root=root)
    sched = {"DAL": {"opponent": "WPG", "game_date": AS_OF}}
    return goalie_mod.estimate(f, sched, cfg, team_goalies={"DAL": [nid]}, as_of=AS_OF)[nid].b2b


# -- the refresh ------------------------------------------------------------------------------------------------------------

def test_back_to_back_flag_fires_once_the_refresh_brings_in_the_first_game(stores, tmp_path):
    _, old = stores
    root = _copy(old, tmp_path)
    assert status.measure(AS_OF, store_root=root).state == "STALE"  # newest regular game 10-07, 3 days before 10-10
    assert _b2b(root) == 0  # the store does not hold the 10-09 start
    cache, transport = _cache(tmp_path)
    res = refresh.refresh_incremental(AS_OF, now=NOW, store_root=root, cache=cache)
    assert res.outcome == "REFRESHED" and res.games_added == 1 and res.rows_added == 8
    assert res.since == date(2025, 10, 5) and res.through == date(2025, 10, 9)
    assert res.last_regular_before == date(2025, 10, 7) and res.last_regular_after == date(2025, 10, 9)
    after = status.measure(AS_OF, store_root=root)
    assert after.state == "CURRENT" and after.current_season_games == 2
    assert _b2b(root) == 1  # Oettinger started the night before: the flag fires on consecutive starts
    assert all("moneypuck" not in u and "boxscore" not in u for u in transport.calls)  # NHL reports only, no cross-check
    assert res.calls == len(transport.calls) == 4 and cache.calls_made == 0  # its own counter; the run's budget is untouched
    sk = store_mod.read("skater_games", [SEASON], root=root)
    assert set(sk["tier"]) == {"B"} and (sk["a1_missing"] == 1).all()  # new rows are Tier B (flag 33)


def test_an_empty_answer_never_erases_stored_games(stores, tmp_path):
    _, old = stores
    root = _copy(old, tmp_path)
    before = _digest(root)
    empty = [("skater/", 200, b'{"data": [], "total": 0}'), ("goalie/", 200, b'{"data": [], "total": 0}')]
    cache, _ = _cache(tmp_path, empty)
    res = refresh.refresh_incremental(AS_OF, now=NOW, store_root=root, cache=cache)
    assert res.outcome == "NO_NEW_GAMES" and res.games_added == 0 and "found nothing" in res.line()
    assert _digest(root) == before  # byte for byte
    assert len(store_mod.read("skater_games", [SEASON], root=root)) == 8  # the 10-07 rows sit inside the re-fetched window


def test_merge_since_upsert_keeps_rows_the_default_replaces():
    old = pd.DataFrame({"nhl_id": [1, 2], "game_id": [10, 11], "game_date": [date(2025, 10, 7), date(2025, 10, 9)], "goals": [0, 1]})
    new = pd.DataFrame({"nhl_id": [2], "game_id": [11], "game_date": [date(2025, 10, 9)], "goals": [2]})
    plain = nhl_reports._merge_since(old, new.iloc[0:0], date(2025, 10, 5))
    assert plain.empty  # the manual `history --since` replaces everything from the date, even with an empty answer
    kept = nhl_reports._merge_since(old, new.iloc[0:0], date(2025, 10, 5), upsert=True)
    assert len(kept) == 2
    merged = nhl_reports._merge_since(old, new, date(2025, 10, 5), upsert=True)
    assert len(merged) == 2 and merged.set_index("nhl_id").loc[2, "goals"] == 2  # the newer row wins
    assert nhl_reports._merge_since(old, new, date(2025, 10, 8)).set_index("nhl_id").loc[2, "goals"] == 2


def test_a_corrected_row_replaces_the_stored_one(stores, tmp_path):
    one, _ = stores
    root = _copy(one, tmp_path)
    body = json.loads((H / "nhl_skater_summary_20251009.json").read_bytes())
    pid = body["data"][0]["playerId"]
    before = int(store_mod.read("skater_games", [SEASON], root=root).set_index("nhl_id").loc[pid, "goals"])
    body["data"][0]["goals"] = body["data"][0]["goals"] + 1
    routes = [("skater/summary", 200, json.dumps(body).encode()), *[r for r in ROUTES if r[0] != "skater/summary"]]
    cache, _ = _cache(tmp_path, routes)
    res = refresh.refresh_incremental(date(2025, 10, 11), now=datetime(2025, 10, 11, 16, 0, tzinfo=timezone.utc), store_root=root,
                                      cache=cache)
    assert res.outcome == "REFRESHED" and res.games_added == 0 and res.rows_added == 0
    assert int(store_mod.read("skater_games", [SEASON], root=root).set_index("nhl_id").loc[pid, "goals"]) == before + 1


def test_the_call_cap_is_its_own_and_a_capped_refresh_leaves_the_store_untouched(stores, tmp_path):
    _, old = stores
    root = _copy(old, tmp_path)
    before = _digest(root)
    cfg = load_sources_config()
    cfg["history"]["refresh_max_calls"] = 2  # a window needs 4 requests
    cache, _ = _cache(tmp_path)
    res = refresh.refresh_incremental(AS_OF, now=NOW, store_root=root, cache=cache, cfg=cfg)
    assert res.outcome == "FAILED" and "the store is unchanged" in res.reason and _digest(root) == before
    assert cache.calls_made == 0


def test_a_slow_answer_times_out_and_the_real_store_never_changes(stores, tmp_path):
    _, old = stores
    root = _copy(old, tmp_path)
    before = _digest(root)
    gate = threading.Event()

    def slow(url):
        gate.wait(5.0)
        return b'{"data": [], "total": 0}'

    cache, _ = _cache(tmp_path, [("skater/", 200, slow), ("goalie/", 200, slow)])
    t0 = time.perf_counter()
    res = refresh.refresh_incremental(AS_OF, now=NOW, store_root=root, cache=cache, budget_s=0.3)
    assert res.outcome == "TIMED_OUT" and time.perf_counter() - t0 < 3 and _digest(root) == before
    gate.set()  # let the abandoned worker finish; it can only ever have written the staging folder
    time.sleep(1.0)
    assert _digest(root) == before


def test_a_refresh_that_returns_less_than_the_store_holds_is_refused(stores, tmp_path, monkeypatch):
    _, old = stores
    root = _copy(old, tmp_path)
    before = _digest(root)
    real = nhl_reports.backfill

    def shrinking(seasons, **kw):
        stats = real(seasons, **kw)
        for kind in ("nhl_skater_games", "skater_games"):  # a staged result that lost a stored row (the oldest, from the 10-07 game)
            df = store_mod.read(kind, seasons, root=kw["store_root"])
            store_mod.write(kind, seasons[0], df.iloc[1:], root=kw["store_root"])
        return stats

    monkeypatch.setattr(nhl_reports, "backfill", shrinking)
    cache, _ = _cache(tmp_path)
    res = refresh.refresh_incremental(AS_OF, now=NOW, store_root=root, cache=cache)
    assert res.outcome == "FAILED" and "lost 1 stored game row(s)" in res.reason and _digest(root) == before


@pytest.mark.parametrize("as_of, now, why", [
    (date(2026, 5, 2), datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc), "regular season is over"),
    (date(2025, 9, 2), datetime(2025, 9, 2, 6, 0, tzinfo=timezone.utc), "no finished day of the season"),
])
def test_no_refresh_outside_the_regular_season(stores, tmp_path, as_of, now, why):
    _, old = stores
    cache, transport = _cache(tmp_path)
    res = refresh.refresh_incremental(as_of, now=now, store_root=old, cache=cache)
    assert res.outcome == "SKIPPED" and why in res.reason and transport.calls == []


# -- inside a run -----------------------------------------------------------------------------------------------------------

def _no_dk(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("no DraftKings in this test")

    monkeypatch.setattr(run_mod, "_fetch_draftables", refuse)  # Phase B reports "draftables unavailable"; the pool is unchanged


def _online(tmp_path, cache, *, clock=lambda: NOW, **kw):
    sal, ent = mini_pair("classic")
    return run_slate(sal, ent, out_root=tmp_path / "runs", outputs_root=tmp_path / "outputs", offline=False, cache=cache,
                     clock=clock, **kw)


def test_a_run_refreshes_after_its_first_publish_and_rebuilds_the_projection(stores, tmp_path, monkeypatch):
    _, old = stores
    root = _copy(old, tmp_path)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root)
    _no_dk(monkeypatch)
    cache, transport = _cache(tmp_path)
    r = _online(tmp_path, cache)
    h = r.manifest["history"]
    assert h["state"] == "STALE" and h["refresh"]["outcome"] == "REFRESHED" and h["refresh"]["games_added"] == 1
    assert h["after"]["state"] == "CURRENT" and h["rebuilt_projection"] is True
    assert r.manifest["model"]["rebuilt_after_history_refresh"] is True
    assert r.statuses["FILE_VALID"] == "TRUE" and r.manifest["versions"][0]["version"] == 1  # v1 was published before the fetch
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    line = next(x for x in notes.splitlines() if x.startswith("- History store: "))
    assert "STALE:" in line and "Refresh after the first publish: REFRESHED: +1 regular-season game(s)" in line
    assert "The store now reads CURRENT" in line and "rebuilt from the refreshed store" in line
    assert len(_reports(transport)) == 4


def test_the_refresh_does_not_change_the_first_file(stores, tmp_path, monkeypatch):
    """v1 is published in Phase A, before any fetch: the same inputs give the same v1 with a refresh and without one."""
    _, old = stores
    root_a, root_b = _copy(old, tmp_path, "a"), _copy(old, tmp_path, "b")
    _no_dk(monkeypatch)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root_a)
    cache_a, _ = _cache(tmp_path / "ca")
    with_refresh = _online(tmp_path / "ra", cache_a)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root_b)
    cache_b, _ = _cache(tmp_path / "cb")
    sal, ent = mini_pair("classic")
    no_refresh = run_slate(sal, ent, out_root=tmp_path / "rb" / "runs", outputs_root=tmp_path / "rb" / "outputs", offline=True,
                           clock=lambda: NOW)
    assert with_refresh.manifest["versions"][0]["sha256"] == no_refresh.manifest["versions"][0]["sha256"]


def test_no_refresh_when_the_store_is_current_absent_or_the_run_is_offline(stores, tmp_path, monkeypatch):
    one, _ = stores
    _no_dk(monkeypatch)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", one)
    cache, transport = _cache(tmp_path / "c1")
    r = _online(tmp_path / "r1", cache, clock=lambda: datetime(2025, 10, 10, 16, 0, tzinfo=timezone.utc))
    assert r.manifest["history"]["state"] == "CURRENT"
    assert r.manifest["history"]["refresh"] == {"outcome": "SKIPPED", "reason": "the store is already current"}
    assert _reports(transport) == []
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", tmp_path / "nowhere")
    cache, transport = _cache(tmp_path / "c2")
    r = _online(tmp_path / "r2", cache)
    assert r.manifest["history"]["state"] == "ABSENT" and r.manifest["history"]["refresh"]["outcome"] == "SKIPPED"
    assert "no store to refresh" in r.manifest["history"]["refresh"]["reason"] and _reports(transport) == []
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", one)
    cache, transport = _cache(tmp_path / "c3")
    sal, ent = mini_pair("classic")
    r = run_slate(sal, ent, out_root=tmp_path / "r3" / "runs", outputs_root=tmp_path / "r3" / "outputs", offline=True, cache=cache,
                  clock=lambda: NOW)
    assert "refresh" not in r.manifest["history"] and _reports(transport) == []


def test_no_refresh_while_the_session_backfill_is_running(stores, tmp_path, monkeypatch):
    import os

    _, old = stores
    root = _copy(old, tmp_path)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root)
    _no_dk(monkeypatch)
    state = tmp_path / "history_bootstrap.state"
    state.write_text(f"state=RUNNING\npid={os.getpid()}\nstarted={NOW:%Y-%m-%dT%H:%M:%SZ}\n", encoding="utf-8")
    monkeypatch.setattr(status, "BOOTSTRAP_STATE", state)
    cache, transport = _cache(tmp_path)
    r = _online(tmp_path, cache)
    assert r.manifest["history"]["refresh"]["outcome"] == "SKIPPED" and "still running" in r.manifest["history"]["refresh"]["reason"]
    assert _reports(transport) == []


def test_no_refresh_inside_the_edit_stop_buffer(stores, tmp_path, monkeypatch):
    _, old = stores
    root = _copy(old, tmp_path)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root)
    _no_dk(monkeypatch)
    cache, transport = _cache(tmp_path)
    r = _online(tmp_path, cache, clock=lambda: datetime(2026, 9, 29, 22, 57, tzinfo=timezone.utc))  # 3 minutes before the first game
    assert "refresh" not in r.manifest["history"] and _reports(transport) == []
    assert any("history refresh skipped" in x for x in r.manifest["messages"])


def test_a_failing_refresh_is_a_message_and_never_stops_the_run(stores, tmp_path, monkeypatch):
    _, old = stores
    root = _copy(old, tmp_path)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", root)
    _no_dk(monkeypatch)
    before = _digest(root)
    cache, _ = _cache(tmp_path, [("skater/", 503, b"down"), ("goalie/", 503, b"down")])
    r = _online(tmp_path, cache)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.manifest["history"]["refresh"]["outcome"] == "FAILED"
    assert any(x.startswith("history refresh FAILED") for x in r.manifest["messages"])
    assert any(x == "history refresh: FAILED" for x in r.manifest["failed"])
    assert _digest(root) == before and "rebuilt_projection" not in r.manifest["history"]


def test_history_refresh_command_reports_before_result_and_after(capsys, stores):
    one, _ = stores
    from nhl_dfs import cli

    rc = cli.main(["history", "--refresh", "--as-of", "2026-05-02", "--store-root", str(one)])
    out = capsys.readouterr().out
    assert rc == 0 and out.count("before: ") == 1 and "result: SKIPPED: the regular season is over" in out and "after: " in out
