from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import TESTS
from nhl_dfs.build.locks import compute
from nhl_dfs.build.notes import chicago
from nhl_dfs.contracts.statuses import CellLock, Eligibility, Participation
from nhl_dfs.data.sources.dk_public import Draftable, Draftables
from nhl_dfs.intake.entries import read_entries
from nhl_dfs.intake.salary import parse_game_info, read_salary

pytestmark = pytest.mark.c2c

LS = TESTS / "fixtures" / "late_swap"
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)  # AAA@BBB 7:00PM EDT
G2 = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)  # CCC@DDD 8:00PM EDT
G3 = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)  # EEE@FFF 10:00PM EDT
BUF = 300


@pytest.fixture(scope="module")
def classic():
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    cur = read_entries(LS / "classic" / "DKEntries.current.csv")
    return pool, cur


def _id(pool, name):
    return next(r.role_id for r in pool.rows if r.name == name)


def _cell(state, pool, eid, name):
    rid = _id(pool, name)
    return next(c for (e, _), c in state.cells.items() if e == eid and c.role_id == rid)


def _draftable(rid, *, start=None, swappable=None, status="None"):
    return Draftable(rid, "p" + rid, "x", "C", 0, 0, status,
                     Participation.OUT if status == "OUT" else Participation.PLAYING,
                     Eligibility.ROSTERABLE, swappable, None, "", None, start)


@pytest.mark.parametrize("now, lock, word", [
    (G1, CellLock.LOCKED, "started"),
    (G1 - timedelta(seconds=BUF), CellLock.EDIT_STOP, "edit stop"),
    (G1 - timedelta(seconds=BUF + 1), CellLock.OPEN, "open"),
])
def test_boundaries(classic, now, lock, word):
    pool, cur = classic
    c = _cell(compute(cur, pool, None, now, BUF), pool, "7100000001", "Alpha C1")
    assert c.lock is lock and c.reason.startswith(word)


def test_edit_stop_never_claims_started(classic):
    pool, cur = classic
    state = compute(cur, pool, None, G1 - timedelta(seconds=60), BUF)
    c = _cell(state, pool, "7100000001", "Alpha C1")
    assert c.lock is CellLock.EDIT_STOP and "started" not in c.reason
    assert state.started_games == frozenset() and state.edit_stop_games == {"AAA@BBB"}
    assert not state.addable(_id(pool, "Alpha C2"))  # inside the buffer: may not be added either


def test_mid_slate_state(classic):
    pool, cur = classic
    now = G1 + timedelta(minutes=10)
    state = compute(cur, pool, None, now, BUF)
    assert state.started_games == {"AAA@BBB"}
    assert _cell(state, pool, "7100000001", "Alpha C1").lock is CellLock.LOCKED
    assert _cell(state, pool, "7100000001", "Echo C1").lock is CellLock.OPEN
    assert all(c.lock is CellLock.OPEN and c.reason == "empty"
               for (e, _), c in state.cells.items() if e == "7100000005")
    assert not state.addable(_id(pool, "Bravo D1")) and state.addable(_id(pool, "Delta D1"))


def test_pinned_uses_canonical_slots_and_bare_ids(classic):
    pool, cur = classic
    state = compute(cur, pool, None, G3 + timedelta(minutes=1), BUF)  # everything started
    pins = state.pinned("7100000002")  # the bare-ID entry
    assert sorted(pins) == list(range(9))
    g = pool.by_role_id[pins[8]]  # canonical 8 is G, although the template has G in column 7
    assert g.is_goalie


def test_participation_is_not_a_lock_input(classic):
    pool, cur = classic
    echo_c1 = _id(pool, "Echo C1")  # OUT in the salary file and in draftables here
    d = Draftables(1, [_draftable(echo_c1, status="OUT")], {})
    c = _cell(compute(cur, pool, d, G1 + timedelta(minutes=1), BUF), pool, "7100000001", "Echo C1")
    assert c.lock is CellLock.OPEN


def test_draftables_start_and_swappable_override(classic):
    pool, cur = classic
    echo_c1, delta_rw2 = _id(pool, "Echo C1"), _id(pool, "Delta RW2")
    early = G1 - timedelta(minutes=30)  # DK moved EEE@FFF earlier than Game Info says
    d = Draftables(1, [_draftable(echo_c1, start=early), _draftable(delta_rw2, swappable=False)], {})
    state = compute(cur, pool, d, G1 - timedelta(minutes=20), BUF)
    assert state.start_source[echo_c1] == "draftables" and state.start_source[delta_rw2] == "game_info"
    assert _cell(state, pool, "7100000001", "Echo C1").lock is CellLock.LOCKED
    c = _cell(state, pool, "7100000001", "Delta RW2")
    assert c.lock is CellLock.LOCKED and c.reason.startswith("not swappable")
    assert not state.addable(delta_rw2)


def test_unreadable_and_unknown_cells_are_pinned(tmp_path, classic):
    pool, _ = classic
    raw = (LS / "classic" / "DKEntries.current.csv").read_bytes()
    raw = raw.replace(b"Alpha LW2 (", b"Alpha LW2 [", 1)  # e1: unreadable cell text
    fox = _id(pool, "Foxtrot C2").encode()
    raw = raw.replace(b"Foxtrot C2 (" + fox + b")", b"Foxtrot C2 (99999999)", 1)  # e3: ID not in the pool
    p = tmp_path / "DKEntries.csv"
    p.write_bytes(raw)
    state = compute(read_entries(p), pool, None, G1 - timedelta(hours=2), BUF)
    bad = {e for (e, _), c in state.cells.items() if c.reason.startswith("unreadable")}
    assert bad == {"7100000001", "7100000003"} == set(state.unreadable_entries)
    assert all(c.lock is CellLock.LOCKED for c in state.cells.values() if c.reason.startswith("unreadable"))


@pytest.mark.parametrize("game_info, zone_name", [
    ("AAA@BBB 10/31/2026 07:00PM ET", "EDT"),  # before DST ends (1 Nov 2026)
    ("AAA@BBB 11/02/2026 07:00PM ET", "EST"),
    ("AAA@BBB 03/09/2026 07:00PM ET", "EDT"),  # after DST starts (8 Mar 2026)
])
def test_dst_and_utc_conversions(game_info, zone_name):
    _, info = parse_game_info(game_info)
    local = datetime.strptime(game_info.split(" ", 1)[1][:-3], "%m/%d/%Y %I:%M%p").replace(tzinfo=ZoneInfo("America/New_York"))
    assert info.start_utc == local.astimezone(timezone.utc) and local.tzname() == zone_name
    shown = chicago(info.start_utc.isoformat())
    assert shown.startswith(local.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d 06:00:00 PM"))
    assert shown.endswith("CDT" if zone_name == "EDT" else "CST")
