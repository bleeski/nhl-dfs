import csv
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import TESTS, mini_pair
from nhl_dfs.build import late_swap
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.contracts.statuses import Eligibility, Participation
from nhl_dfs.data.sources.dk_public import Draftable, Draftables
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary
from pool_builder import clone_entries

pytestmark = pytest.mark.c2c

LS = TESTS / "fixtures" / "late_swap"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
G2 = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
G3 = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)


def base(tmp_path, kind="classic"):
    d = LS / kind
    r = run_slate(d / "DKSalaries.csv", d / "DKEntries.template.csv", offline=True,
                  out_root=tmp_path / "runs", clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def swap(tmp_path, b, current, *, as_of, **kw):
    kw.setdefault("offline", True)
    return late_swap.run(b.run.run_id, current, runs_root=tmp_path / "runs", as_of=as_of, **kw)


def cells(path) -> dict[str, tuple[str, ...]]:
    return {e.entry_id: e.cells for e in read_entries(path).entries}


def ids(path) -> dict[str, set[str]]:
    return {e.entry_id: {cell_role_id(c) for c in e.cells} - {None} for e in read_entries(path).entries}


def names(pool, rids):
    return {pool.by_role_id[r].name for r in rids}


def pool_of(kind="classic"):
    return read_salary(LS / kind / "DKSalaries.csv")


def without_entry(src: Path, dst: Path, entry_id: str) -> Path:
    raw = src.read_bytes()
    lines = raw.split(b"\r\n")
    dst.write_bytes(b"\r\n".join(x for x in lines if not x.lstrip(b"\xef\xbb\xbf").startswith(entry_id.encode())))
    return dst


def test_all_locked_file_returns_unchanged_bytes(tmp_path):
    b = base(tmp_path)
    cur = without_entry(LS / "classic" / "DKEntries.current.csv", tmp_path / "cur.csv", "7100000005")
    r = swap(tmp_path, b, cur, as_of=G3 + timedelta(minutes=5))
    assert r.statuses["FILE_VALID"] == "TRUE"
    assert r.run.version_file(1).read_bytes() == cur.read_bytes()
    assert r.manifest["changed_cells"] == [] and any("nothing new to upload" in m for m in r.messages)
    assert r.manifest["locks"]["counts"] == {"LOCKED": 36}


def test_partial_lock_changes_only_open_cells_and_reports_pinned_over_cap(tmp_path):
    b = base(tmp_path)
    cur = LS / "classic" / "DKEntries.current.csv"
    now = G1 + timedelta(minutes=10)
    r = swap(tmp_path, b, cur, as_of=now)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.manifest["versions"]
    before, after = cells(cur), cells(r.run.version_file(1))
    pool = pool_of()
    g1_ids = {x.role_id for x in pool.rows if x.team in ("AAA", "BBB")}
    for eid, row in before.items():
        for k, text in enumerate(row):
            rid = cell_role_id(text)
            if rid in g1_ids:  # locked: exact bytes kept, bare-ID cells included
                assert after[eid][k] == text
    changed = {(c["entry_id"], c["to"]) for c in r.manifest["changed_cells"]}
    assert {e for e, _ in changed} == {"7100000001", "7100000005"}  # the OUT player's entry and the blank one
    over = r.manifest["pinned_over_cap"]
    assert over == [{"name": "Alpha C1", "entries": 4, "cap": 3}]
    assert r.manifest["manual_changes"], "current export differs from the delivered baseline"


def test_started_player_is_never_added(tmp_path):
    b = base(tmp_path)
    now = G1 + timedelta(minutes=10)
    r = swap(tmp_path, b, LS / "classic" / "DKEntries.current.csv", as_of=now)
    pool = pool_of()
    for c in r.manifest["changed_cells"]:
        team = pool.by_role_id[c["to"]].team
        assert team not in ("AAA", "BBB"), c  # game 1 has started
    echo_c1 = next(x.role_id for x in pool.rows if x.name == "Echo C1")
    assert echo_c1 not in ids(r.run.version_file(1))["7100000001"]  # OUT player replaced
    e5 = [pool.by_role_id[x] for x in ids(r.run.version_file(1))["7100000005"]]
    assert len(e5) == 9 and check_lineup_any(e5)


def check_lineup_any(rows):
    """Legality regardless of slot order: the referee already checked the written cells."""
    return len({r.person_key for r in rows}) == len(rows) and sum(r.salary for r in rows) <= 50_000


def test_edit_stop_cells_are_pinned_and_reported_as_edit_stop(tmp_path):
    b = base(tmp_path)
    now = G1 - timedelta(seconds=60)
    r = swap(tmp_path, b, LS / "classic" / "DKEntries.current.csv", as_of=now)
    assert r.manifest["locks"]["started_games"] == [] and r.manifest["locks"]["edit_stop_games"] == ["AAA@BBB"]
    assert r.manifest["locks"]["counts"].get("EDIT_STOP") == 11 and "LOCKED" not in r.manifest["locks"]["counts"]
    msg = next(m for m in r.messages if m.startswith("edit stop"))
    assert "not started" in msg and "has started" not in msg
    pool = pool_of()
    for c in r.manifest["changed_cells"]:
        assert pool.by_role_id[c["to"]].team not in ("AAA", "BBB")  # inside the buffer: not added


def test_expensive_goalie_replacement_forces_a_two_player_repair(tmp_path):
    # B1: a re-download may not change an original row's salary, so the expensive goalies are priced in the run's own
    # salary file and the fresh file differs from it only by Echo G1's Status (a valid re-download)
    src = LS / "classic" / "DKSalaries.csv"
    text = src.read_bytes().decode("utf-8-sig")

    def priced(out: bool, dst):
        rows = list(csv.reader(io.StringIO(text, newline="")))
        for rec in rows[1:]:
            if rec and rec[2] == "Echo G1" and out:
                rec[9] = "OUT"
            elif rec and rec[2] in ("Echo G2", "Foxtrot G1", "Foxtrot G2"):
                rec[5] = "9900"
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator="\r\n").writerows(rows)
        dst.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
        return dst

    b = run_slate(priced(False, tmp_path / "DKSalaries.priced.csv"), LS / "classic" / "DKEntries.template.csv",
                  offline=True, out_root=tmp_path / "runs", clock=lambda: BEFORE)
    assert b.statuses["FILE_VALID"] == "TRUE"
    fresh = priced(True, tmp_path / "DKSalaries.fresh.csv")
    cur = without_entry(LS / "classic" / "DKEntries.current.csv", tmp_path / "cur.csv", "7100000005")
    r = swap(tmp_path, b, cur, as_of=G2 + timedelta(minutes=10), salary_path=fresh)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages
    e4 = [c for c in r.manifest["changed_cells"] if c["entry_id"] == "7100000004"]
    assert len(e4) == 2  # the goalie and exactly one skater
    pool = read_salary(fresh)
    lineup = [pool.by_role_id[x] for x in ids(r.run.version_file(1))["7100000004"]]
    assert sum(x.salary for x in lineup) <= 50_000
    assert {x.name for x in lineup if x.is_goalie} <= {"Echo G2", "Foxtrot G1", "Foxtrot G2"}


def test_two_game_showdown_with_game_one_locked_resolves_game_two(tmp_path):
    b = base(tmp_path, "showdown2")
    r = swap(tmp_path, b, LS / "showdown2" / "DKEntries.current.csv", as_of=G1 + timedelta(minutes=10))
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages
    pool = pool_of("showdown2")
    out = read_entries(r.run.version_file(1))
    by_id = {e.entry_id: [pool.by_role_id[cell_role_id(c)] for c in e.cells] for e in out.entries}
    for eid, rows in by_id.items():
        assert check_lineup(rows, Mode.SHOWDOWN).ok
    for c in r.manifest["changed_cells"]:
        assert pool.by_role_id[c["to"]].team in ("CCC", "DDD")
    assert "Delta C1" not in {x.name for x in by_id["7200000002"]}  # OUT, open, replaced
    assert {x.team for x in by_id["7200000003"]} <= {"CCC", "DDD"}  # blank entry filled from game 2
    assert cells(LS / "showdown2" / "DKEntries.current.csv")["7200000001"] == cells(r.run.version_file(1))["7200000001"]


def test_showdown_pins_from_one_team_force_the_other_team(tmp_path, monkeypatch):
    b = base(tmp_path, "showdown2")
    pool = pool_of("showdown2")
    cur = LS / "showdown2" / "DKEntries.current.csv"
    s1 = read_entries(cur).entries[0]
    pinned = [cell_role_id(c) for c in s1.cells[:4]]  # CPT + 3 FLEX, all AAA
    stub = Draftables(1, [Draftable(rid, "p", "x", "C", 0, 0, "None", Participation.PLAYING, Eligibility.ROSTERABLE,
                                    False, None, "AAA", None, None) for rid in pinned], {})
    monkeypatch.setattr(late_swap, "fetch_draftables_bounded", lambda *a, **k: (stub, "stub draftables"))
    # before any game: only isSwappable pins them, so AAA teammates are still addable
    r = swap(tmp_path, b, cur, as_of=BEFORE, offline=False, fast=False)
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages
    rows = [pool.by_role_id[cell_role_id(c)] for c in read_entries(r.run.version_file(1)).entries[0].cells]
    assert [x.role_id for x in rows[:4]] == pinned
    assert len({x.team for x in rows}) >= 2 and any(x.team != "AAA" for x in rows[4:])


def test_non_fast_reoptimizes_open_cells_with_distinct_entries(tmp_path):
    b = base(tmp_path)
    r = swap(tmp_path, b, LS / "classic" / "DKEntries.current.csv", as_of=G1 + timedelta(minutes=10), fast=False)
    assert r.statuses["FILE_VALID"] == "TRUE"
    acts = {e["entry_id"]: e["action"] for e in r.manifest["entries"]}
    assert set(acts.values()) == {"reoptimized"} and len(acts) == 5
    sets = [frozenset(v) for v in ids(r.run.version_file(1)).values()]
    assert len(set(sets)) == 5


def test_lock_boundary_crossing_mid_compute_keeps_the_predecessor(tmp_path, monkeypatch):
    b = base(tmp_path)
    public_before = b.public_path.read_bytes()
    now = {"t": G1 + timedelta(minutes=10)}
    real = late_swap._solve_entry

    def slow_solve(*a, **k):
        now["t"] = G3 + timedelta(minutes=1)  # every game starts while we compute
        return real(*a, **k)

    monkeypatch.setattr(late_swap, "_solve_entry", slow_solve)
    r = late_swap.run(b.run.run_id, LS / "classic" / "DKEntries.current.csv", offline=True,
                      runs_root=tmp_path / "runs", clock=lambda: now["t"])
    assert r.statuses["FILE_VALID"] == "FALSE" and r.run.version_numbers() == []
    assert any("lock boundary crossed" in m for m in r.messages)
    assert b.public_path.read_bytes() == public_before


def test_unreadable_cell_leaves_the_entry_and_blocks_an_unchecked_file(tmp_path):
    b = base(tmp_path)
    raw = (LS / "classic" / "DKEntries.current.csv").read_bytes().replace(b"Alpha LW2 (", b"Alpha LW2 [", 1)
    cur = tmp_path / "cur.csv"
    cur.write_bytes(raw)
    r = swap(tmp_path, b, cur, as_of=G1 + timedelta(minutes=10))
    assert any("7100000001" in m and "could not be read" in m for m in r.messages)
    assert r.statuses["FILE_VALID"] == "FALSE" and r.run.version_numbers() == []


def test_a_salary_file_from_another_slate_is_refused(tmp_path):
    b = base(tmp_path)
    r = swap(tmp_path, b, LS / "classic" / "DKEntries.current.csv", as_of=G1, salary_path=mini_pair("classic")[0])
    assert r.statuses["DELIVERY_STATUS"] == "FAILED" and any("different slate" in m for m in r.messages)


def test_150_entries_late_swap_is_fast(tmp_path):
    b = base(tmp_path)
    many = tmp_path / "cur.csv"
    clone_entries(LS / "classic" / "DKEntries.current.csv", many, 150)
    t0 = time.perf_counter()
    r = swap(tmp_path, b, many, as_of=G1 + timedelta(minutes=10))
    assert time.perf_counter() - t0 <= 30.0
    assert r.statuses["FILE_VALID"] == "TRUE", r.messages[:3]


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_post_lock_export_cells_are_readable(mode):
    """DK's current-entries export AFTER lock has never been seen; the late-swap rules assume its
    cells are "Name (ID)" or bare IDs. Skips loudly until Ben saves one real post-lock export as
    tests/fixtures/real/<date>/<mode>/DKEntries.postlock.csv next to that slate's DKSalaries.csv."""
    import warnings

    from conftest import REAL
    from nhl_dfs.build.locks import compute

    hits = sorted(REAL.glob(f"*/{mode}/DKEntries.postlock.csv"))
    if not hits:
        msg = f"REAL FIXTURE MISSING: tests/fixtures/real/<date>/{mode}/DKEntries.postlock.csv (a DK export after lock)"
        warnings.warn(msg)
        pytest.skip(msg)
    path = hits[-1]
    pool = read_salary(path.parent / "DKSalaries.csv")
    cur = read_entries(path)
    state = compute(cur, pool, None, datetime.now(timezone.utc), 300)
    assert not state.unreadable_entries, "post-lock cell format differs from the assumed Name (ID) / bare ID"
