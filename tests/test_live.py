"""C9: live.condition. No reliable standings snapshot exists yet, so the normal path is a no-op with a
status; a reliable one conditions started games and classifies each entry."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np
import pytest

from nhl_dfs.build import live
from nhl_dfs.build import objectives as ob
from nhl_dfs.contracts.statuses import PayoutSource
from nhl_dfs.intake.entries import read_entries
from test_late_swap_objective import MID, _setup, _swap, _v3, full_classic  # noqa: F401 (module fixture)

pytestmark = pytest.mark.c9

NOW = datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)


def _scen():
    base = np.array([[10, 50, 70], [20, 0, 30], [40, 100, 0]], np.int32)
    return ob.ScenarioSet(["r1", "r2", "r3"], base, "selection", 1)


def _pool():
    rows = {"r1": NS(team="AAA"), "r2": NS(team="BBB"), "r3": NS(team="CCC")}
    games = {"BBB@AAA": NS(home="AAA", away="BBB"), "DDD@CCC": NS(home="CCC", away="DDD")}
    return NS(by_role_id=rows, games=games)


def _contest(cash_line=2):
    return ob.Contest("c1", "x", "cash", 4, 100, np.array([180] * cash_line, np.int64), np.zeros(cash_line, bool), None,
                      PayoutSource.PRIOR, "")


def _snap(**kw):
    d = dict(as_of_utc=NOW - timedelta(minutes=3), source="DK standings export (test)",
             entries={"e1": live.EntryStanding("e1", "c1", 120, 3), "e2": live.EntryStanding("e2", "c1", 300, 1)},
             players={"r1": 25}, game_progress={"BBB@AAA": 0.5, "DDD@CCC": 1.0})
    d.update(kw)
    return live.StandingsSnapshot(**d)


def test_no_snapshot_is_a_no_op_with_a_status():
    s = _scen()
    got = live.condition(s, None)
    assert got.scenarios is s and got.status == live.NO_SNAPSHOT and got.entry_state == {}
    assert "no chase pivot" in got.notes[0]


@pytest.mark.parametrize("change, why", [
    ({"source": ""}, "source not declared"),
    ({"as_of_utc": NOW - timedelta(minutes=40)}, "min old"),
    ({"entries": {"e1": live.EntryStanding("e1", "c1", 120, 3)}}, "missing"),
])
def test_an_unreliable_snapshot_changes_nothing(change, why):
    s = _scen()
    got = live.condition(s, _snap(**change), pool=_pool(), contests={"c1": _contest()}, now=NOW,
                         needed_entries=["e1", "e2"])
    assert got.scenarios is s and got.status == live.UNRELIABLE and got.entry_state == {}
    assert why in got.notes[0]


def test_a_reliable_snapshot_conditions_started_games_and_classifies_entries():
    got = live.condition(_scen(), _snap(), pool=_pool(), contests={"c1": _contest(cash_line=2)}, now=NOW,
                         needed_entries=["e1", "e2"])
    assert got.status == live.CONDITIONED
    b = got.scenarios.base
    assert b[:, 0].tolist() == [30, 35, 45]  # observed 25 + half the draw (game half played)
    assert b[:, 1].tolist() == [25, 0, 50]  # same game, no observation: 0 so far + half the draw
    assert b[:, 2].tolist() == [0, 0, 0]  # a final game: the observation alone (none recorded: 0)
    assert got.entry_state == {"e1": "TRAILING", "e2": "AHEAD"}


def test_the_trailing_policy_applies_only_with_a_snapshot():
    from nhl_dfs.build.swap_objective import ScenarioObjective
    from nhl_dfs.models.contests import load_contest_families

    so = ScenarioObjective.__new__(ScenarioObjective)
    so.cache = NS(contests={"c1": NS(family="large_gpp")})
    so.fam_cfg = load_contest_families()
    so.live = None
    assert so.policy("e1", "c1") == "own_then_dup"
    so.live = live.Conditioned(_scen(), live.CONDITIONED, {"e1": "TRAILING", "e2": "AHEAD"})
    assert so.policy("e1", "c1") == "dup_first" and so.policy("e2", "c1") == "mean"
    assert so.policy("e3", "c1") == "own_then_dup"


def _live_snapshot(r, as_of, rank):
    ids = [e.entry_id for e in read_entries(_v3(r)).entries]
    return live.StandingsSnapshot(as_of - timedelta(minutes=2), "test export",
                                  {e: live.EntryStanding(e, str(x.contest_id), 0, rank)
                                   for e, x in zip(ids, read_entries(_v3(r)).entries)},
                                  {}, {"NYI@CAR": 0.3})


def test_no_chase_pivot_without_a_reliable_snapshot(full_classic, tmp_path):  # noqa: F811
    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    plain = _swap(tmp, r.run.run_id, _v3(r), as_of=MID, salary=fresh, fast=False)
    stale = _live_snapshot(r, MID - timedelta(hours=2), rank=10_000)  # everything "trailing", but 2 hours old
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=MID, salary=fresh, fast=False, standings=stale)
    assert plain.ok and s.ok
    assert s.manifest["live"]["LIVE_STATUS"] == live.UNRELIABLE
    assert s.run.version_file(1).read_bytes() == plain.run.version_file(1).read_bytes()
    assert plain.manifest["live"]["LIVE_STATUS"] == live.NO_SNAPSHOT
    picks = [e["selection"] for e in plain.manifest["objective"]["entries"].values() if e["selection"].get("candidates")]
    assert picks and all(p["policy"] in ("own_then_dup", "mean") for p in picks)  # the families' own policies


def test_a_trailing_entry_prefers_lower_duplication_only_with_a_snapshot(full_classic, tmp_path):  # noqa: F811
    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    snap = _live_snapshot(r, MID, rank=10_000)
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=MID, salary=fresh, fast=False, standings=snap)
    assert s.ok and s.manifest["live"]["LIVE_STATUS"] == live.CONDITIONED
    rec = s.manifest["objective"]["scenario"]["live"]
    assert set(rec["entry_state"].values()) == {"TRAILING"}
    picks = [e["selection"] for e in s.manifest["objective"]["entries"].values() if e["selection"].get("candidates")]
    assert picks and all(p["policy"] == "dup_first" for p in picks)
