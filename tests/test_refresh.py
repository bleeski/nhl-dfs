import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TESTS
from nhl_dfs import cli
from nhl_dfs.build import late_swap, refresh
from nhl_dfs.build.run import run_slate
from nhl_dfs.contracts.statuses import Eligibility, Participation
from nhl_dfs.data.sources.dk_public import Draftable, Draftables
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary

pytestmark = pytest.mark.c2c

LS = TESTS / "fixtures" / "late_swap"
BEFORE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
MID = G1 + timedelta(minutes=10)


@pytest.fixture
def base(tmp_path):
    d = LS / "classic"
    r = run_slate(d / "DKSalaries.csv", d / "DKEntries.template.csv", offline=True,
                  out_root=tmp_path / "runs", clock=lambda: BEFORE)
    assert r.statuses["FILE_VALID"] == "TRUE"
    return r


def _roster(path, eid):
    e = next(x for x in read_entries(path).entries if x.entry_id == eid)
    return [cell_role_id(c) for c in e.cells]


def test_refresh_uses_the_last_delivered_version_as_an_assumed_parent(tmp_path, base):
    r = refresh.run(base.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=MID)
    assert r.manifest["kind"] == "refresh" and r.manifest["assumed_parent"] is True
    assert r.manifest["parent_file"]["sha256"] == base.manifest["export_sha256"]
    assert r.statuses["FILE_VALID"] == "TRUE" and r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    assert any("assumed parent" in m and "NOT the live account" in m for m in r.messages)
    assert r.manifest["manual_changes"] == []


def _fresh_with_out(tmp_path, delivered, eid="7100000001"):
    """A re-downloaded salary file marking one open (late-game) rostered skater OUT."""
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    late = {x.role_id for x in pool.rows if x.team in ("EEE", "FFF") and not x.is_goalie}
    victim = next(rid for rid in _roster(delivered, eid) if rid in late)
    text = (LS / "classic" / "DKSalaries.csv").read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    for rec in rows[1:]:
        if rec and rec[3] == victim:
            rec[9] = "OUT"
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator=chr(13) + chr(10)).writerows(rows)
    fresh = tmp_path / "DKSalaries.csv"
    fresh.write_bytes(bytes([0xEF, 0xBB, 0xBF]) + buf.getvalue().encode("utf-8"))
    return fresh, victim


def test_refresh_applies_the_same_pins_and_repairs_as_late_swap(tmp_path, base):
    delivered = base.run.version_file(1)
    fresh, _ = _fresh_with_out(tmp_path, delivered)
    a = late_swap.run(base.run.run_id, delivered, offline=True, fast=True, runs_root=tmp_path / "runs",
                      as_of=MID, salary_path=fresh)
    b = refresh.run(base.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=MID, salary_path=fresh)
    assert b.manifest["parent_file"]["sha256"] == base.manifest["export_sha256"]  # rehearsal never moved published.json
    assert a.manifest["locks"] == b.manifest["locks"]
    assert a.manifest["changed_cells"] and a.manifest["changed_cells"] == b.manifest["changed_cells"]
    assert a.run.version_file(1).read_bytes() == b.run.version_file(1).read_bytes()


def test_rehearsal_never_touches_the_public_file(tmp_path, base):
    public = base.public_path
    stamp = public.parent / "published.json"
    before, stamp_before = public.read_bytes(), stamp.read_bytes()
    r = late_swap.run(base.run.run_id, LS / "classic" / "DKEntries.current.csv", offline=True, fast=True,
                      runs_root=tmp_path / "runs", as_of=MID)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.run.version_numbers() == [1]
    assert r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    assert public.read_bytes() == before and stamp.read_bytes() == stamp_before
    assert str(r.run.path) in r.manifest["public_path"] and any("do not upload" in m for m in r.messages)


def test_real_clock_late_swap_publishes_to_outputs(tmp_path, base):
    r = late_swap.run(base.run.run_id, LS / "classic" / "DKEntries.current.csv", offline=True, fast=True,
                      runs_root=tmp_path / "runs", clock=lambda: MID)
    assert r.public_path == base.public_path and r.statuses["DELIVERY_STATUS"] == "CHECKED"
    assert r.public_path.read_bytes() == r.run.version_file(1).read_bytes()


def test_refresh_with_a_fresh_salary_file_repairs_a_new_out(tmp_path, base):
    delivered = base.run.version_file(1)
    fresh, victim = _fresh_with_out(tmp_path, delivered)
    r = refresh.run(base.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=MID, salary_path=fresh)
    assert r.statuses["FILE_VALID"] == "TRUE"
    out = r.run.version_file(1)
    assert all(victim not in _roster(out, e.entry_id) for e in read_entries(out).entries)
    assert any(c["from"] == victim for c in r.manifest["changed_cells"])


def test_refresh_online_uses_draftables_status(tmp_path, base, monkeypatch):
    delivered = base.run.version_file(1)
    pool = read_salary(LS / "classic" / "DKSalaries.csv")
    late = [rid for rid in _roster(delivered, "7100000002") if pool.by_role_id[rid].team in ("EEE", "FFF")]
    victim = late[0]
    stub = Draftables(1, [Draftable(victim, "p", "x", "C", 0, 0, "OUT", Participation.OUT, Eligibility.ROSTERABLE,
                                    True, None, pool.by_role_id[victim].team, None, None)], {})
    monkeypatch.setattr(late_swap, "fetch_draftables_bounded", lambda *a, **k: (stub, "stub draftables"))
    r = refresh.run(base.run.run_id, offline=False, runs_root=tmp_path / "runs", as_of=MID)
    assert r.statuses["NEWS_STATE"] == "PARTIAL"  # the stub lists one row
    assert victim not in _roster(r.run.version_file(1), "7100000002")


def test_refresh_without_any_delivered_version_raises(tmp_path, base):
    for p in (base.run.path / "current", base.public_path.parent / "published.json"):
        p.unlink()
    with pytest.raises(refresh.NoDeliveredVersion):
        refresh.run(base.run.run_id, offline=True, runs_root=tmp_path / "runs", as_of=MID)


def test_cli_late_swap_and_refresh(tmp_path, base, capsys):
    runs = str(tmp_path / "runs")
    cur = str(LS / "classic" / "DKEntries.current.csv")
    as_of = "2026-10-15T23:10:00Z"
    assert cli.main(["late-swap", "--run", base.run.run_id, "--entries", cur, "--fast", "--offline",
                     "--as-of", as_of, "--runs-root", runs]) == 0
    out = capsys.readouterr().out
    assert "FILE_VALID=TRUE" in out and "REHEARSAL CLOCK" in out and "fast repair" in out
    assert cli.main(["refresh", "--run", base.run.run_id, "--offline", "--as-of", as_of, "--runs-root", runs]) == 0
    assert "assumed parent" in capsys.readouterr().out
    # rehearsal runs are named by the real clock, never the rehearsal time
    assert all(not d.name.startswith("20261015-2310") for d in (tmp_path / "runs").iterdir())
    assert cli.main(["late-swap", "--run", base.run.run_id]) == 2
    with pytest.raises(SystemExit):
        cli.main(["refresh", "--run", base.run.run_id, "--as-of", "2026-10-15T23:10:00", "--runs-root", runs])
