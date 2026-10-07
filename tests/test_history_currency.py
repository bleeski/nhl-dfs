"""C39 (B70, B71): how current the history store is, reported in RUN_NOTES and the manifest, and (later commits) kept current.

The fixture store is the recorded 2025-10-09 game of tests/test_nhl_reports.py (8 skaters, 2 goalies) so every state is
built from real report rows, not hand-made frames. conftest points the default store at an empty folder, so a test that wants
a store says so with monkeypatch.
"""

import shutil
from datetime import date, datetime, timezone

import pytest

from conftest import mini_pair
from nhl_dfs import cli
from nhl_dfs.build.run import run_slate
from nhl_dfs.data.history import status
from nhl_dfs.data.history import store as store_mod
from test_nhl_reports import SEASON, run

pytestmark = pytest.mark.c39

BEFORE = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)  # the mini fixture's slate is 2026-09-29; as-of 2026-09-28


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c39store")
    run(tmp, moneypuck=False, sub="b")  # one regular-season game, 2025-10-09
    return tmp / "b" / "store"


# -- the states ------------------------------------------------------------------------------------------------------------

def test_current_stale_and_prior_season_states(fixture_store):
    cur = status.measure(date(2025, 10, 10), store_root=fixture_store)
    assert cur.state == "CURRENT" and cur.days_behind == 1 and cur.current_season_games == 1
    assert cur.last_regular == date(2025, 10, 9) == cur.last_game and cur.goalie_last_regular == date(2025, 10, 9)
    edge = status.measure(date(2025, 10, 11), store_root=fixture_store)  # 10-10 has no game stored: two days behind is STALE
    assert edge.state == "STALE" and edge.days_behind == 2
    stale = status.measure(date(2025, 10, 20), store_root=fixture_store)
    assert stale.state == "STALE" and stale.days_behind == 11
    new_season = status.measure(date(2026, 9, 28), store_root=fixture_store)
    assert new_season.state == "PRIOR_SEASON_ONLY" and new_season.current_season_games == 0
    assert new_season.last_regular == date(2025, 10, 9)
    over = status.measure(date(2026, 5, 2), store_root=fixture_store)
    assert over.state == "SEASON_COMPLETE"


def test_absent_when_no_store_and_unreadable_when_a_file_is_corrupt(tmp_path, fixture_store):
    empty = status.measure(date(2026, 9, 28), store_root=tmp_path / "nothing")
    assert empty.state == "ABSENT" and empty.seasons == [] and empty.last_game is None
    broken = tmp_path / "broken"
    shutil.copytree(fixture_store, broken)
    store_mod.path_for("skater_games", SEASON, root=broken).write_bytes(b"not a parquet file")
    bad = status.measure(date(2026, 9, 28), store_root=broken)  # never raises
    assert bad.state == "UNREADABLE" and bad.error
    assert "UNREADABLE" in bad.line() and "priors" in bad.line()


def test_only_the_regimes_the_model_reads_make_a_store_current(tmp_path, fixture_store):
    """A preseason or playoff row must not make the store look current (model.yaml regimes: [regular])."""
    root = tmp_path / "pre"
    shutil.copytree(fixture_store, root)
    for kind in ("skater_games", "goalie_games"):
        df = store_mod.read(kind, [SEASON], root=root)
        store_mod.write(kind, SEASON, df.assign(regime="preseason"), root=root)
    st = status.measure(date(2025, 10, 10), store_root=root)
    assert st.state == "PRIOR_SEASON_ONLY" and st.last_regular is None and st.last_game == date(2025, 10, 9)
    assert st.days_behind is None and "no regular-season game of any season is stored" in st.line()
    po = tmp_path / "po"
    shutil.copytree(fixture_store, po)
    for kind in ("skater_games", "goalie_games"):
        df = store_mod.read(kind, [SEASON], root=po)
        store_mod.write(kind, SEASON, df.assign(regime="playoffs"), root=po)
    assert status.measure(date(2025, 10, 10), store_root=po).state == "PRIOR_SEASON_ONLY"


def test_the_line_says_what_is_missing_in_plain_words(fixture_store):
    line = status.measure(date(2026, 9, 28), store_root=fixture_store).line({"counts": {"PRIOR": 7, "HISTORY": 2, "MIXED": 1}})
    assert line.startswith("PRIOR_SEASON_ONLY: no 2026-27 regular-season game is stored")
    assert "newest regular-season game 2025-10-09 (354 day(s) before the as-of date 2026-09-28)" in line
    assert "back-to-back goalie flag cannot see any game after that" in line
    assert "Seasons stored: 2025-26" in line and "Model inputs: HISTORY 2, MIXED 1, PRIOR 7 of 10 persons." in line
    cur = status.measure(date(2025, 10, 10), store_root=fixture_store).line()
    assert cur.startswith("CURRENT: newest regular-season game 2025-10-09 (1 day(s) before the as-of date 2025-10-10); "
                          "1 2025-26 regular-season game(s) stored.")


def test_a_day_is_complete_three_hours_after_midnight_eastern():
    et2 = datetime(2026, 10, 6, 6, 30, tzinfo=timezone.utc)  # 02:30 ET on 10-06: yesterday's late games may still be on
    et3 = datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc)  # 03:30 ET
    assert status.completed_through(et2) == date(2026, 10, 4)
    assert status.completed_through(et3) == date(2026, 10, 5)
    assert status.completed_through(datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)) == date(2026, 10, 5)  # 19:00 ET


# -- RUN_NOTES and the manifest ----------------------------------------------------------------------------------------------

def _offline(tmp_path, sub):
    sal, ent = mini_pair("classic")
    return run_slate(sal, ent, out_root=tmp_path / sub / "runs", outputs_root=tmp_path / sub / "outputs", offline=True,
                     clock=lambda: BEFORE)


def test_run_notes_and_manifest_carry_the_staleness_line_on_a_fixture_store(tmp_path, fixture_store, monkeypatch):
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", fixture_store)
    r = _offline(tmp_path, "a")
    h = r.manifest["history"]
    assert h["state"] == "PRIOR_SEASON_ONLY" and h["as_of"] == "2026-09-28" and h["last_regular_game"] == "2025-10-09"
    assert h["days_behind"] == 354 and h["seasons"] == [SEASON]
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    line = next(x for x in notes.splitlines() if x.startswith("- History store: "))
    assert "PRIOR_SEASON_ONLY: no 2026-27 regular-season game is stored" in line and "2025-10-09" in line
    assert "Model inputs:" in line  # the model counts ride on the same line
    assert [x for x in notes.splitlines() if x.startswith("- DK status")] and notes.index("- DK status") < notes.index("- History store")


def test_a_default_run_with_no_store_says_absent(tmp_path):
    r = _offline(tmp_path, "a")  # conftest points the default store at an empty folder
    assert r.manifest["history"]["state"] == "ABSENT"
    notes = (r.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")
    assert "- History store: ABSENT: no history store on this machine" in notes


def test_the_measurement_cannot_change_the_file_and_never_stops_a_run(tmp_path, fixture_store, monkeypatch):
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", fixture_store)
    with_line = _offline(tmp_path, "a")

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(status, "measure", boom)
    without = _offline(tmp_path, "b")
    assert with_line.manifest["versions"][-1]["sha256"] == without.manifest["versions"][-1]["sha256"]
    assert with_line.public_path.read_bytes() == without.public_path.read_bytes()
    assert "history" not in without.manifest and without.statuses["FILE_VALID"] == "TRUE"
    assert any("history store status unavailable (RuntimeError" in x for x in without.manifest["messages"])
    assert "- History store:" not in (without.run.path / "RUN_NOTES.md").read_text(encoding="utf-8")


def test_phase_a_reads_only_local_files(tmp_path, fixture_store, monkeypatch):
    """The measurement makes no network call: conftest refuses a real transport and this refuses sockets as well."""
    import socket

    def refuse(*a, **k):
        raise AssertionError("network call attempted by the staleness measurement")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(store_mod, "DEFAULT_ROOT", fixture_store)
    assert _offline(tmp_path, "a").manifest["history"]["state"] == "PRIOR_SEASON_ONLY"


def test_history_status_command_prints_the_same_line(fixture_store, capsys):
    rc = cli.main(["history", "--status", "--as-of", "2025-10-10", "--store-root", str(fixture_store)])
    out = capsys.readouterr().out
    assert rc == 0 and "history store status as of 2025-10-10" in out and "CURRENT: newest regular-season game 2025-10-09" in out


def test_config_carries_the_documented_numbers():
    from nhl_dfs.data.http import load_sources_config

    h = load_sources_config()["history"]
    assert h["fresh_days"] == 1 and h["complete_after_h"] == 3 and h["bootstrap_stale_min"] == 30
