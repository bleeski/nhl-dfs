import json
import math
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import mini_pair, real_pair
from nhl_dfs.build import run as run_mod
from nhl_dfs.build.manifest import REQUIRED_KEYS, STATUS_KEYS
from nhl_dfs.build.run import run_slate, salary_statuses, slate_id_for
from nhl_dfs.build.state import sha256
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.data.http import SourceUnavailable
from nhl_dfs.intake.entries import cell_role_id, read_entries
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.referee.check_file import check_file
from pool_builder import clone_entries

pytestmark = pytest.mark.c2b

# Every run in this suite is pinned before the 2026-09-29 slates start, so the suite keeps
# passing after that date (next_chunk.py reruns it every session).
BEFORE = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
DATE = "2026-09-29"


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call attempted during an offline run")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _rostered(path) -> set[str]:
    """Role IDs in the entry rows' roster cells (not the embedded player list)."""
    ef = read_entries(path)
    return {cell_role_id(c) for e in ef.entries for c in e.cells} - {None}


def _run(tmp_path, salary, entries, **kw):
    kw.setdefault("offline", True)
    kw.setdefault("clock", lambda: BEFORE)
    return run_slate(salary, entries, out_root=tmp_path / "runs", **kw)


def _status_col_edit(src: Path, dst: Path, edits: dict[str, str]) -> None:
    """Copy a salary file, setting the Status cell of the given IDs (Status is the second-last column)."""
    raw = src.read_bytes()
    crlf, lf = bytes([13, 10]), bytes([10])
    sep = crlf if crlf in raw else lf
    lines = raw.split(sep)
    out = []
    for line in lines:
        parts = line.split(b",")
        # ID is column 3 unless a quoted comma shifts it; the mini/real files have none in these rows
        if len(parts) > 4 and parts[3].decode() in edits:
            parts[-2] = edits[parts[3].decode()].encode()
        out.append(b",".join(parts))
    dst.write_bytes(sep.join(out))


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_offline_real_run_is_valid_and_never_touches_the_network(tmp_path, no_network, mode):
    salary, entries = real_pair(mode, DATE)
    r = _run(tmp_path, salary, entries)
    assert r.statuses["FILE_VALID"] == "TRUE" and r.statuses["NEWS_STATE"] == "NONE"
    assert r.statuses["MODEL_STATUS"] == "PRIOR" and r.statuses["SEARCH_STATUS"] == "FEASIBLE"
    assert r.public_path is not None and r.public_path.exists()
    assert r.public_path.read_bytes() == r.run.version_file(1).read_bytes()
    assert (r.run.path / "RUN_NOTES.md").exists() and (r.run.path / "manifest.json").exists()
    # the referee, run fresh against the ORIGINAL uploaded files, agrees
    assert check_file(r.public_path, salary, entries).ok


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_manifest_fields_and_hashes_match_the_bytes(tmp_path, mode):
    salary, entries = real_pair(mode, DATE)
    r = _run(tmp_path, salary, entries)
    m = json.loads(r.manifest_path.read_text())
    assert all(k in m for k in REQUIRED_KEYS) and all(k in m["statuses"] for k in STATUS_KEYS)
    assert m["salary_sha256"] == sha256(Path(salary).read_bytes()) == sha256((r.run.inputs / "DKSalaries.csv").read_bytes())
    assert m["entries_sha256"] == sha256(Path(entries).read_bytes()) == sha256((r.run.inputs / "DKEntries.csv").read_bytes())
    assert m["export_sha256"] == sha256(r.public_path.read_bytes()) == m["versions"][-1]["sha256"]
    assert m["versions"][0]["version"] == 1 and m["versions"][0]["phase"] == "A"
    assert m["entry_count"] == len(m["entry_fees"]) and m["slate_id"] == slate_id_for(read_salary(salary))
    assert m["exposures_top20"] and "total_s" in m["phase_timings"]


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_out_and_ir_players_are_never_rostered(tmp_path, mode):
    salary, entries = real_pair(mode, DATE)
    r = _run(tmp_path, salary, entries)
    pool = read_salary(salary)
    st = salary_statuses(pool)
    out_people = {pool.by_role_id[rid].person_key for rid, (p, _) in st.items() if p is Participation.OUT}
    assert out_people, "fixture should contain OUT/IR players"
    used = _rostered(r.public_path)
    assert used and not {pool.by_role_id[rid].person_key for rid in used} & out_people


def test_unknown_status_is_kept_and_reported_and_out_is_excluded(tmp_path):
    salary, entries = mini_pair("classic")
    pool = read_salary(salary)
    skaters = [r for r in pool.rows if not r.is_goalie]
    weird, gone = skaters[0], skaters[1]
    edited = tmp_path / "DKSalaries.csv"
    _status_col_edit(Path(salary), edited, {weird.role_id: "SUSP", gone.role_id: "OUT"})
    st = salary_statuses(read_salary(edited))
    assert st[weird.role_id] == (Participation.UNKNOWN, "SUSP") and st[gone.role_id][0] is Participation.OUT
    r = _run(tmp_path, edited, entries)
    assert r.statuses["FILE_VALID"] == "TRUE"
    assert r.statuses["DELIVERY_STATUS"] == "DEGRADED_REVIEW"
    assert any("UNKNOWN" in msg and "SUSP" in msg for msg in r.messages)
    assert gone.role_id not in _rostered(r.public_path)


def test_a_started_slate_is_refused(tmp_path):
    salary, entries = mini_pair("classic")
    r = _run(tmp_path, salary, entries, clock=lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
    assert r.statuses["FILE_VALID"] == "FALSE" and r.statuses["DELIVERY_STATUS"] == "FAILED"
    assert r.public_path is None and r.run.version_numbers() == []
    assert any("late swap" in msg for msg in r.messages)
    assert (r.run.path / "RUN_NOTES.md").exists()


def test_mode_mismatch_is_refused(tmp_path):
    r = _run(tmp_path, mini_pair("classic")[0], mini_pair("showdown")[1])
    assert r.statuses["DELIVERY_STATUS"] == "FAILED" and r.run.version_numbers() == []


def test_a_failing_referee_report_blocks_publish(tmp_path, monkeypatch):
    real = run_mod.check_file

    def failing(*a, **k):
        rep = real(*a, **k)
        rep.ok, rep.reasons = False, ["injected failure"]
        return rep

    monkeypatch.setattr(run_mod, "check_file", failing)
    r = _run(tmp_path, *mini_pair("classic"))
    assert r.statuses["FILE_VALID"] == "FALSE" and r.public_path is None
    assert r.run.version_numbers() == [] and not (tmp_path / "outputs").exists()


def test_phase_a_publishes_before_any_phase_b_call(tmp_path, monkeypatch):
    order = []
    real_publish = run_mod.publish

    def spy_publish(*a, **k):
        order.append("publish")
        return real_publish(*a, **k)

    def spy_fetch(*a, **k):
        order.append("fetch")
        raise SourceUnavailable("stub outage")

    monkeypatch.setattr(run_mod, "publish", spy_publish)
    monkeypatch.setattr(run_mod, "_fetch_draftables", spy_fetch)
    r = _run(tmp_path, *mini_pair("showdown"), offline=False)
    assert order == ["publish", "fetch"]
    assert r.statuses["FILE_VALID"] == "TRUE"


def test_rerun_on_the_same_slate_replaces_the_public_file(tmp_path):
    salary, entries = mini_pair("classic")
    a = _run(tmp_path, salary, entries)
    b = _run(tmp_path, salary, entries, seed=999)
    assert a.public_path == b.public_path and a.run.run_id != b.run.run_id
    assert json.loads((a.public_path.parent / "published.json").read_text())["run_id"] == b.run.run_id


@pytest.mark.parametrize("mode", ["classic", "showdown"])
@pytest.mark.parametrize("n, limit_s", [(20, 15.0), (150, 30.0)])
def test_phase_a_timing_on_the_real_fixture(tmp_path, mode, n, limit_s):
    salary, entries = real_pair(mode, DATE)
    many = tmp_path / "DKEntries.csv"
    clone_entries(entries, many, n)
    t0 = time.perf_counter()
    r = _run(tmp_path, salary, many)
    elapsed = time.perf_counter() - t0
    assert r.statuses["FILE_VALID"] == "TRUE" and r.manifest["entry_count"] == n
    assert r.manifest["phase_timings"]["phase_a_s"] <= limit_s and elapsed <= limit_s, elapsed
    assert r.manifest["distinct_lineups"] == n and not r.manifest["relaxations"]
    if mode == "classic":
        assert r.manifest["overlap_max"] <= 7
    cap = max(1, math.ceil(0.6 * n))
    assert max(p["entries"] for p in r.manifest["exposures_top20"]) <= cap


@pytest.fixture
def pinned_cli(monkeypatch):
    """cli run with the clock pinned before the slate (the CLI itself uses the real clock)."""
    import functools

    from nhl_dfs import cli

    monkeypatch.setattr(run_mod, "run_slate", functools.partial(run_mod.run_slate, clock=lambda: BEFORE))
    return cli


def test_cli_run_verify_and_status(tmp_path, pinned_cli, capsys):
    cli = pinned_cli
    salary, entries = mini_pair("showdown")
    runs = tmp_path / "runs"
    # C3 changed `run` without --baseline from "refused" to "baseline, then the provisional pass";
    # --baseline still means baseline only. Both stay inside tmp_path.
    roots = ["--runs-root", str(tmp_path / "full" / "runs"), "--outputs-root", str(tmp_path / "full" / "outputs")]
    assert cli.main(["run", "--salary", str(salary), "--entries", str(entries), "--offline", *roots]) == 0
    capsys.readouterr()
    code = cli.main(["run", "--salary", str(salary), "--entries", str(entries), "--baseline", "--offline",
                     "--runs-root", str(runs)])
    out = capsys.readouterr().out
    assert code == 0 and "FILE_VALID=TRUE" in out and "published: " in out
    run_dir = next(p for p in runs.iterdir() if p.is_dir())
    assert len(list(run_dir.glob("versions/v*"))) == 1  # baseline only
    run_id = next(line.split()[0][4:] for line in out.splitlines() if line.startswith("run="))
    assert cli.main(["verify", "--run", run_id, "--runs-root", str(runs)]) == 0
    assert "FILE_VALID=TRUE" in capsys.readouterr().out
    lines = cli.last_run_lines(runs)
    assert run_id in lines[0] and "FILE_VALID=TRUE" in lines[1]
    # tampering with the published version is caught
    vfile = runs / run_id / "versions" / "v1" / "DKEntries.csv"
    vfile.write_bytes(vfile.read_bytes() + b"\n")
    assert cli.main(["verify", "--run", run_id, "--runs-root", str(runs)]) == 1
    assert cli.main(["verify", "--run", "nope-classic", "--runs-root", str(runs)]) == 1


def test_status_with_no_runs(tmp_path):
    from nhl_dfs import cli

    assert cli.last_run_lines(tmp_path / "none") == ["last run: none"]


def test_a_busy_slate_lock_is_reported_not_raised(tmp_path, monkeypatch):
    from nhl_dfs.build.state import LockTimeout

    def busy(*a, **k):
        raise LockTimeout("could not lock outputs/x/.lock within 60s")

    monkeypatch.setattr(run_mod, "publish", busy)
    r = _run(tmp_path, *mini_pair("classic"))
    assert r.statuses["FILE_VALID"] == "FALSE" and r.statuses["DELIVERY_STATUS"] == "FAILED"
    assert any("another run on this slate" in msg for msg in r.messages)
    assert (r.run.path / "RUN_NOTES.md").exists() and (r.run.path / "manifest.json").exists()
